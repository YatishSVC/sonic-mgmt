import logging
import pytest
import json

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.gu_utils import apply_patch, expect_op_success, expect_op_failure
from tests.common.gu_utils import generate_tmpfile, delete_tmpfile
from tests.common.gu_utils import format_json_patch_for_multiasic
from tests.common.gu_utils import create_checkpoint, delete_checkpoint, rollback_or_reload
from tests.common.gu_utils import is_valid_platform_and_version

pytestmark = [
    pytest.mark.asic('mellanox', 'marvell-teralynx'),
    pytest.mark.topology('any'),
]

logger = logging.getLogger(__name__)

READ_FLEXCOUNTER_DB_TIMEOUT = 480
READ_FLEXCOUNTER_DB_INTERVAL = 20


@pytest.fixture(scope="module")
def ensure_dut_readiness(duthost):
    """
    Setup/teardown fixture for pfcwd interval config update tst

    Args:
        duthost: DUT host object
    """
    create_checkpoint(duthost)

    yield

    try:
        logger.info("Rolled back to original  checkpoint")
        rollback_or_reload(duthost)
    finally:
        delete_checkpoint(duthost)


@pytest.fixture(autouse=True, scope="module")
def enable_default_pfcwd_configuration(duthost):
    res = duthost.shell('redis-dump -d 4 --pretty -k \"DEVICE_METADATA|localhost\"')
    meta_data = json.loads(res["stdout"])
    pfc_status = meta_data["DEVICE_METADATA|localhost"]["value"].get("default_pfcwd_status", "")
    if pfc_status == 'disable':
        duthost.shell('redis-cli -n 4 hset \"DEVICE_METADATA|localhost\" default_pfcwd_status enable')
        # apply cofig to all namespaces also for multi-asic platforms
        for asic_id in duthost.get_asic_ids():
            if asic_id:
                duthost.asic_instance(asic_id).command(
                    'redis-cli -n 4 hset \"DEVICE_METADATA|localhost\" default_pfcwd_status enable'
                )
    # Enable default pfcwd configuration
    start_pfcwd = duthost.shell('config pfcwd start_default')
    pytest_assert(not start_pfcwd['rc'], "Failed to start default pfcwd config")
    # apply cofig to all namespaces also for multi-asic platforms
    for asic_id in duthost.get_asic_ids():
        if asic_id:
            start_pfcwd = duthost.asic_instance(asic_id).command('config pfcwd start_default')
            pytest_assert(not start_pfcwd['rc'], "Failed to start default pfcwd config for asic")


def ensure_application_of_updated_config(duthost, value, cli_namespace_prefix):
    """
    Ensures application of the JSON patch config update by verifying field value presence in FLEX COUNTER DB

    Args:
        duthost: DUT host object
        value: expected value of POLL_INTERVAL
        cli_namespace_prefix: fixture for the formatted cli namespace
    """
    def _confirm_value_in_flex_counter_db():
        cmd = 'sonic-db-cli {} PFC_WD_DB hget FLEX_COUNTER_GROUP_TABLE:PFC_WD POLL_INTERVAL'.format(
            cli_namespace_prefix)
        poll_interval = duthost.shell(cmd)["stdout"]
        return value == poll_interval

    pytest_assert(
        wait_until(READ_FLEXCOUNTER_DB_TIMEOUT, READ_FLEXCOUNTER_DB_INTERVAL, 0, _confirm_value_in_flex_counter_db),
        "FLEX COUNTER DB does not properly reflect newly POLL_INTERVAL expected value: {}".format(value)
    )


def prepare_pfcwd_interval_config(duthost, value, ip_netns_namespace_prefix, cli_namespace_prefix):
    """
    Prepares config db by setting pfcwd poll interval to specified value.
    If value is empty string, delete the current entry.

    Args:
        duthost: DUT host object
        value: poll interval value to be set
        ip_netns_namespace_prefix: fixture for the formatted ip netns namespace
        cli_namespace_prefix: fixture for the formatted cli namespace
    """
    logger.info("Setting configdb entry pfcwd poll interval to value: {}".format(value))

    if value:
        cmd = "{} pfcwd interval {}".format(ip_netns_namespace_prefix, value)
    else:
        cmd = r"sonic-db-cli {} CONFIG_DB del \PFC_WD\GLOBAL\POLL_INTERVAL".format(cli_namespace_prefix)

    duthost.shell(cmd)


def get_detection_restoration_times(duthost, ip_netns_namespace_prefix, cli_namespace_prefix):
    """
    Returns detection_time, restoration_time for an interface.
    Poll_interval must be greater than both in order to be valid

    Args:
        duthost: DUT host object
        ip_netns_namespace_prefix: fixture for the formatted ip netns namespace
        cli_namespace_prefix: fixture for the formatted cli namespace
    """

    cmd = '{} config pfcwd start --action drop all 400 --restoration-time 400'.format(
        ip_netns_namespace_prefix)
    duthost.shell(cmd, module_ignore_errors=True)
    pfcwd_config = duthost.shell("show pfcwd config")
    pytest_assert(not pfcwd_config['rc'], "Unable to read pfcwd config")

    for line in pfcwd_config['stdout_lines']:
        if line.startswith('Ethernet'):
            interface = line.split()[0]     # Since line starts with Ethernet, we can safely use 0 index
            cmd = "sonic-db-cli {} CONFIG_DB hget \"PFC_WD|{}\" \"detection_time\" ".format(
                cli_namespace_prefix, interface)
            output = duthost.shell(cmd, module_ignore_errors=True)
            pytest_assert(not output['rc'], "Unable to read detection time")
            detection_time = output["stdout"]

            cmd = "sonic-db-cli {} CONFIG_DB hget \"PFC_WD|{}\" \"restoration_time\" ".format(
                cli_namespace_prefix, interface)
            output = duthost.shell(cmd, module_ignore_errors=True)
            pytest_assert(not output['rc'], "Unable to read restoration time")
            restoration_time = output["stdout"]

            return int(detection_time), int(restoration_time)

    pytest_assert(True, "Failed to read detection_time and/or restoration time")


def get_new_interval(duthost, is_valid, ip_netns_namespace_prefix, cli_namespace_prefix):
    """
    Returns new interval value for pfcwd poll interval, based on the operation being performed

    Args:
        duthost: DUT host object
        is_valid: if is_valid is true, return a valid new interval. Config update should succeed.
        ip_netns_namespace_prefix: fixture for the formatted ip netns namespace
        cli_namespace_prefix: fixture for the formatted cli namespace
        If is_valid is false, return an invalid new interval. Config update should fail.
    """

    detection_time, restoration_time = get_detection_restoration_times(duthost, ip_netns_namespace_prefix,
                                                                       cli_namespace_prefix)
    if is_valid:
        return max(detection_time, restoration_time) - 10
    else:
        return min(detection_time, restoration_time) + 10


@pytest.mark.parametrize("oper", ["add", "replace"])
@pytest.mark.parametrize("field_pre_status", ["existing", "nonexistent"])
@pytest.mark.parametrize("is_valid_config_update", [True, False])
def test_pfcwd_interval_config_updates(duthost, ensure_dut_readiness, oper,
                                       field_pre_status, is_valid_config_update,
                                       enum_rand_one_frontend_asic_index,
                                       ip_netns_namespace_prefix,
                                       cli_namespace_prefix,
                                       loganalyzer):
    asic_namespace = duthost.get_namespace_from_asic_id(enum_rand_one_frontend_asic_index)

    if not is_valid_config_update and loganalyzer and loganalyzer[duthost.hostname]:
        ignore_regex_list = [
            ".*ERR.*Data Loading Failed:detection_time must be greater than or equal to POLL_INTERVAL.*"
        ]
        loganalyzer[duthost.hostname].ignore_regex.extend(ignore_regex_list)

    new_interval = get_new_interval(duthost, is_valid_config_update, ip_netns_namespace_prefix,
                                    cli_namespace_prefix)

    operation_to_new_value_map = {"add": "{}".format(new_interval), "replace": "{}".format(new_interval)}
    detection_time, restoration_time = get_detection_restoration_times(duthost, ip_netns_namespace_prefix,
                                                                       cli_namespace_prefix)
    pre_status = max(detection_time, restoration_time)
    field_pre_status_to_value_map = {"existing": "{}".format(pre_status), "nonexistent": ""}

    prepare_pfcwd_interval_config(duthost, field_pre_status_to_value_map[field_pre_status],
                                  ip_netns_namespace_prefix, cli_namespace_prefix)

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {} created for json patch of pfcwd poll interval and operation: {}".format(tmpfile, oper))
    value = operation_to_new_value_map[oper]
    logger.info("value to be added to json patch: {}".format(value))

    json_patch = [
        {
            "op": "{}".format(oper),
            "path": "/PFC_WD/GLOBAL/POLL_INTERVAL",
            "value": "{}".format(value)
        }]
    json_patch = format_json_patch_for_multiasic(duthost=duthost, json_data=json_patch,
                                                 is_asic_specific=True, asic_namespaces=[asic_namespace])

    try:
        output = apply_patch(duthost, json_data=json_patch, dest_file=tmpfile)

        if is_valid_config_update and is_valid_platform_and_version(duthost, "PFC_WD", "PFCWD enable/disable", oper):
            expect_op_success(duthost, output)
            ensure_application_of_updated_config(duthost, value, cli_namespace_prefix)
        else:
            expect_op_failure(output)
    finally:
        delete_tmpfile(duthost, tmpfile)
