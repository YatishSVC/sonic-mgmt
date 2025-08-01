import pytest

from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert as py_assert
from tests.dhcp_relay.dhcp_relay_utils import check_routes_to_dhcp_server

SINGLE_TOR_MODE = 'single'
DUAL_TOR_MODE = 'dual'


def pytest_addoption(parser):
    """
    Adds options to pytest that are used by the COPP tests.
    """
    parser.addoption(
        "--stress_restart_round",
        action="store",
        type=int,
        default=10,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--stress_restart_duration",
        action="store",
        type=int,
        default=90,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--stress_restart_pps",
        action="store",
        type=int,
        default=100,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--max_packets_per_sec",
        action="store",
        type=int,
        help="Set maximum packets per second for stress test",
    )


@pytest.fixture(scope="module", autouse=True)
def check_dhcp_feature_status(duthost):
    feature_status_output = duthost.show_and_parse("show feature status")
    for feature in feature_status_output:
        if feature["feature"] == "dhcp_relay" and feature["state"] != "enabled":
            pytest.skip("dhcp_relay is not enabled")


@pytest.fixture(scope="module")
def dut_dhcp_relay_data(duthosts, rand_one_dut_hostname, ptfhost, tbinfo):
    """ Fixture which returns a list of dictionaries where each dictionary contains
        data necessary to test one instance of a DHCP relay agent running on the DuT.
        This fixture is scoped to the module, as the data it gathers can be used by
        all tests in this module. It does not need to be run before each test.
    """
    duthost = duthosts[rand_one_dut_hostname]
    dhcp_relay_data_list = []

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    switch_loopback_ip = mg_facts['minigraph_lo_interfaces'][0]['addr']

    # SONiC spawns one DHCP relay agent per VLAN interface configured on the DUT
    vlan_dict = mg_facts['minigraph_vlans']
    for vlan_iface_name, vlan_info_dict in list(vlan_dict.items()):
        # Filter(remove) PortChannel interfaces from VLAN members list
        vlan_members = [port for port in vlan_info_dict['members'] if 'PortChannel' not in port]
        # Gather information about the downlink VLAN interface this relay agent is listening on
        downlink_vlan_iface = {}
        downlink_vlan_iface['name'] = vlan_iface_name

        for vlan_interface_info_dict in mg_facts['minigraph_vlan_interfaces']:
            if vlan_interface_info_dict['attachto'] == vlan_iface_name:
                downlink_vlan_iface['addr'] = vlan_interface_info_dict['addr']
                downlink_vlan_iface['mask'] = vlan_interface_info_dict['mask']
                break

        # Obtain MAC address of the VLAN interface
        res = duthost.shell('cat /sys/class/net/{}/address'.format(vlan_iface_name))
        downlink_vlan_iface['mac'] = res['stdout']

        downlink_vlan_iface['dhcp_server_addrs'] = mg_facts['dhcp_servers']

        # We choose the physical interface where our DHCP client resides to be index of first interface
        # with alias (ignore PortChannel) in the VLAN
        client_iface = {}
        for port in vlan_members:
            if port in mg_facts['minigraph_port_name_to_alias_map']:
                break
        else:
            continue
        client_iface['name'] = port
        client_iface['alias'] = mg_facts['minigraph_port_name_to_alias_map'][client_iface['name']]
        client_iface['port_idx'] = mg_facts['minigraph_ptf_indices'][client_iface['name']]

        # Obtain uplink port indices for this DHCP relay agent
        uplink_interfaces = []
        uplink_port_indices = []
        for iface_name, neighbor_info_dict in list(mg_facts['minigraph_neighbors'].items()):
            if neighbor_info_dict['name'] in mg_facts['minigraph_devices']:
                neighbor_device_info_dict = mg_facts['minigraph_devices'][neighbor_info_dict['name']]
                if 'type' in neighbor_device_info_dict and neighbor_device_info_dict['type'] in \
                        ['LeafRouter', 'MgmtLeafRouter', 'BackEndLeafRouter']:
                    # If this uplink's physical interface is a member of a portchannel interface,
                    # we record the name of the portchannel interface here, as this is the actual
                    # interface the DHCP relay will listen on.
                    iface_is_portchannel_member = False
                    for portchannel_name, portchannel_info_dict in list(mg_facts['minigraph_portchannels'].items()):
                        if 'members' in portchannel_info_dict and iface_name in portchannel_info_dict['members']:
                            iface_is_portchannel_member = True
                            if portchannel_name not in uplink_interfaces:
                                uplink_interfaces.append(portchannel_name)
                            break
                    # If the uplink's physical interface is not a member of a portchannel,
                    # add it to our uplink interfaces list
                    if not iface_is_portchannel_member:
                        uplink_interfaces.append(iface_name)
                    uplink_port_indices.append(mg_facts['minigraph_ptf_indices'][iface_name])

        other_client_ports_indices = []
        for iface_name in vlan_members:
            if mg_facts['minigraph_ptf_indices'][iface_name] == client_iface['port_idx']:
                pass
            else:
                other_client_ports_indices.append(mg_facts['minigraph_ptf_indices'][iface_name])

        dhcp_relay_data = {}
        dhcp_relay_data['downlink_vlan_iface'] = downlink_vlan_iface
        dhcp_relay_data['client_iface'] = client_iface
        dhcp_relay_data['other_client_ports'] = other_client_ports_indices
        dhcp_relay_data['uplink_interfaces'] = uplink_interfaces
        dhcp_relay_data['uplink_port_indices'] = uplink_port_indices
        dhcp_relay_data['switch_loopback_ip'] = str(switch_loopback_ip)
        dhcp_relay_data['portchannels'] = mg_facts['minigraph_portchannels']
        dhcp_relay_data['vlan_members'] = vlan_members

        # Obtain MAC address of an uplink interface because vlan mac may be different than that of physical interfaces
        res = duthost.shell('cat /sys/class/net/{}/address'.format(uplink_interfaces[0]))
        dhcp_relay_data['uplink_mac'] = res['stdout']
        dhcp_relay_data['default_gw_ip'] = mg_facts['minigraph_mgmt_interface']['gwaddr']

        dhcp_relay_data_list.append(dhcp_relay_data)

    return dhcp_relay_data_list


@pytest.fixture(scope="module")
def validate_dut_routes_exist(duthosts, rand_one_dut_hostname, dut_dhcp_relay_data):
    """Fixture to valid a route to each DHCP server exist
    """
    py_assert(wait_until(360, 5, 0, check_routes_to_dhcp_server, duthosts[rand_one_dut_hostname],
                         dut_dhcp_relay_data),
              "Packets relayed to DHCP server should go through default route via upstream neighbor, but now it's" +
              " going through mgmt interface, which means device is in an unhealthy status")


@pytest.fixture(scope="module")
def testing_config(duthosts, rand_one_dut_hostname, tbinfo):
    duthost = duthosts[rand_one_dut_hostname]

    if 'dualtor' in tbinfo['topo']['name']:
        yield DUAL_TOR_MODE, duthost
    else:
        yield SINGLE_TOR_MODE, duthost


@pytest.fixture(scope="function")
def clean_processes_after_stress_test(ptfhost):
    yield
    ptfhost.shell("kill -9 $(ps aux | grep  dhcp_relay_stress_test | grep -v 'grep' | awk '{print $2}')",
                  module_ignore_errors=True)
