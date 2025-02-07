#
# Copyright (C) 2024-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import time
from test.pylib.manager_client import ManagerClient
import pytest
from test.pylib.rest_client import inject_error, inject_error_one_shot
from test.pylib.util import read_barrier, unique_name, wait_for_cql_and_get_hosts


"""
Tests case when bigger auth operation is split into multiple raft commands.
"""
@pytest.mark.asyncio
async def test_auth_raft_command_split(manager: ManagerClient) -> None:
    servers = await manager.servers_add(3)

    cql = manager.get_cql()
    hosts = await wait_for_cql_and_get_hosts(cql, servers, time.time() + 60)
    await manager.servers_see_each_other(servers)

    initial_perms = await cql.run_async("SELECT * FROM system_auth_v2.role_permissions")

    shared_role = "shared_role_" + unique_name()
    await cql.run_async(f"CREATE ROLE IF NOT EXISTS {shared_role}")

    users = ["user_" + unique_name() for _ in range(30)]
    for user in users:
        # if not exists due to https://github.com/scylladb/python-driver/issues/296
        await cql.run_async(f"CREATE ROLE IF NOT EXISTS {user}")
        await cql.run_async(f"GRANT ALL ON ROLE {shared_role} TO {user}")

    # this will trigger cascade of deletes which should be packed
    # into raft commands in a way that none exceeds max_command_size
    await manager.driver_connect(server=servers[0])
    cql = manager.get_cql()
    async with inject_error(manager.api, servers[0].ip_addr,
                            'auth_announce_mutations_command_max_size'):
        await cql.run_async(f"DROP ROLE IF EXISTS {shared_role}", execution_profile='whitelist')

    # auth reads are eventually consistent so we need to sync all nodes
    await asyncio.gather(*(read_barrier(cql, host) for host in hosts))

    # confirm that deleted shared_role is not attached to any other role
    assert await cql.run_async(f"SELECT * FROM system_auth_v2.role_permissions WHERE resource = 'role/{shared_role}' ALLOW FILTERING") == []

    # cleanup
    for user in users:
        await cql.run_async(f"DROP ROLE IF EXISTS {user}")
    await asyncio.gather(*(read_barrier(cql, host) for host in hosts))
    current_perms = await cql.run_async("SELECT * FROM system_auth_v2.role_permissions")
    assert initial_perms == current_perms
