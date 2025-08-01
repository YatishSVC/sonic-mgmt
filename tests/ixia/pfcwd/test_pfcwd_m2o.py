import pytest

from tests.common.helpers.assertions import pytest_require
from tests.common.fixtures.conn_graph_facts import conn_graph_facts, fanout_graph_facts     # noqa: F401
from tests.common.ixia.ixia_fixtures import ixia_api_serv_ip, ixia_api_serv_port,\
    ixia_api_serv_user, ixia_api_serv_passwd, ixia_api, ixia_testbed_config                 # noqa: F401
from tests.common.ixia.qos_fixtures import prio_dscp_map, all_prio_list,\
    lossless_prio_list, lossy_prio_list                                                     # noqa: F401

from .files.pfcwd_multi_node_helper import run_pfcwd_multi_node_test
from .files.helper import skip_pfcwd_test

pytestmark = [pytest.mark.topology('tgen')]


@pytest.mark.parametrize("trigger_pfcwd", [True, False])
def test_pfcwd_many_to_one(ixia_api, ixia_testbed_config, conn_graph_facts, fanout_graph_facts,     # noqa: F811
                           duthosts, rand_one_dut_hostname, setup_cgm_alpha_cisco,
                           rand_one_dut_portname_oper_up, rand_one_dut_lossless_prio,
                           lossy_prio_list, prio_dscp_map, trigger_pfcwd):                          # noqa: F811

    """
    Run PFC watchdog test under many to one traffic pattern

    Args:
        ixia_api (pytest fixture): IXIA session
        ixia_testbed_config (pytest fixture): testbed configuration information
        conn_graph_facts (pytest fixture): connection graph
        fanout_graph_facts (pytest fixture): fanout graph
        duthosts (pytest fixture): list of DUTs
        rand_one_dut_hostname (str): hostname of DUT
        rand_one_dut_portname_oper_up (str): port to test, e.g., 's6100-1|Ethernet0'
        rand_one_dut_lossless_prio (str): lossless priority to test, e.g., 's6100-1|3'
        lossy_prio_list (pytest fixture): list of lossy priorities
        prio_dscp_map (pytest fixture): priority vs. DSCP map (key = priority)
        trigger_pfcwd (bool): if PFC watchdog is expected to be triggered

    Returns:
        N/A
    """
    dut_hostname, dut_port = rand_one_dut_portname_oper_up.split('|')
    dut_hostname2, lossless_prio = rand_one_dut_lossless_prio.split('|')
    pytest_require(rand_one_dut_hostname == dut_hostname == dut_hostname2,
                   "Priority and port are not mapped to the expected DUT")

    duthost = duthosts[rand_one_dut_hostname]
    skip_pfcwd_test(duthost=duthost, trigger_pfcwd=trigger_pfcwd)

    testbed_config, port_config_list = ixia_testbed_config
    lossless_prio = int(lossless_prio)

    run_pfcwd_multi_node_test(api=ixia_api,
                              testbed_config=testbed_config,
                              port_config_list=port_config_list,
                              conn_data=conn_graph_facts,
                              fanout_data=fanout_graph_facts,
                              duthost=duthost,
                              dut_port=dut_port,
                              pause_prio_list=[lossless_prio],
                              test_prio_list=[lossless_prio],
                              bg_prio_list=lossy_prio_list,
                              prio_dscp_map=prio_dscp_map,
                              trigger_pfcwd=trigger_pfcwd,
                              pattern="many to one")
