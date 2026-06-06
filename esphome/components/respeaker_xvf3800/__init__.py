import hashlib
from pathlib import Path
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation, core, external_files
from esphome.components import i2c, switch, text_sensor, sensor, number, select
from esphome.const import (
    CONF_ID,
    CONF_INITIAL_VALUE,
    CONF_ON_ERROR,
    CONF_RAW_DATA_ID,
    CONF_TRIGGER_ID,
    CONF_URL,
    CONF_VERSION
)
from esphome.core import HexInt

# Dependency declarations
DEPENDENCIES = ["i2c"]
AUTO_LOAD = ["switch", "text_sensor", "sensor", "number", "select"]
CODEOWNERS = ["@formatBCE"]

# Configuration keys
CONF_MUTE_SWITCH = "mute_switch"
CONF_DFU_VERSION = "dfu_version"
CONF_LED_BEAM_SENSOR = "led_beam_sensor"
CONF_FIRMWARE = "firmware"
CONF_MD5 = "md5"
CONF_ON_BEGIN = "on_begin"
CONF_ON_END = "on_end"
CONF_ON_PROGRESS = "on_progress"
CONF_TUNING = "tuning"

DOMAIN = "respeaker_xvf3800"

# XVF3800 post-processing (PP) / Audio-Mgr tuning parameters, each exposed as a
# live-tunable number entity. Verified against Respeaker xvf_host.py command map
# and the XMOS XVF3800 user guide.
#   key: (resid, cmd, is_int, min, max, step, default, icon, description)
TUNING_PARAMS = {
    "stationary_noise_suppression":     (17, 21, False, 0.0,  1.0,    0.01,  0.15, "mdi:waveform",      "PP_MIN_NS: stationary noise floor (lower = more suppression)"),
    "non_stationary_noise_suppression": (17, 22, False, 0.0,  1.0,    0.01,  0.51, "mdi:waveform",      "PP_MIN_NN: non-stationary noise floor"),
    "agc_enabled":                      (17, 10, True,  0,    1,      1,     1,    "mdi:tune-variant",  "PP_AGCONOFF: automatic gain control on/off"),
    "agc_max_gain":                     (17, 11, False, 1.0,  1000.0, 1.0,   1000.0, "mdi:volume-plus", "PP_AGCMAXGAIN: maximum AGC gain factor"),
    "agc_desired_level":                (17, 12, False, 0.0,  1.0,    0.001, 0.003, "mdi:target",       "PP_AGCDESIREDLEVEL: AGC target output level"),
    "attns_mode":                       (17, 32, True,  0,    2,      1,     0,    "mdi:tune-variant",  "PP_ATTNS_MODE: extra non-speech attenuation (0/1/2)"),
    "attns_nominal":                    (17, 33, False, 1.0,  10.0,   0.1,   1.0,  "mdi:tune",          "PP_ATTNS_NOMINAL: non-speech attenuation at nominal level"),
    "attns_slope":                      (17, 34, False, 1.0,  10.0,   0.1,   1.0,  "mdi:tune",          "PP_ATTNS_SLOPE: non-speech attenuation slope"),
}

# Create a namespace for the component
respeaker_xvf3800_ns = cg.esphome_ns.namespace('respeaker_xvf3800')
RespeakerXVF3800 = respeaker_xvf3800_ns.class_('RespeakerXVF3800', cg.Component, i2c.I2CDevice)
RespeakerXVF3800FlashAction = respeaker_xvf3800_ns.class_("RespeakerXVF3800FlashAction", automation.Action)

MuteSwitch = respeaker_xvf3800_ns.class_('MuteSwitch', switch.Switch, cg.PollingComponent)
DFUVersionTextSensor = respeaker_xvf3800_ns.class_('DFUVersionTextSensor', text_sensor.TextSensor, cg.PollingComponent)
LEDBeamSensor = respeaker_xvf3800_ns.class_('LEDBeamSensor', sensor.Sensor, cg.PollingComponent)
XVF3800ParamNumber = respeaker_xvf3800_ns.class_('XVF3800ParamNumber', number.Number, cg.Component)

DFUEndTrigger = respeaker_xvf3800_ns.class_("DFUEndTrigger", automation.Trigger.template())
DFUErrorTrigger = respeaker_xvf3800_ns.class_("DFUErrorTrigger", automation.Trigger.template())
DFUProgressTrigger = respeaker_xvf3800_ns.class_(
    "DFUProgressTrigger", automation.Trigger.template()
)
DFUStartTrigger = respeaker_xvf3800_ns.class_("DFUStartTrigger", automation.Trigger.template())


def _compute_local_file_path(url: str) -> Path:
    h = hashlib.new("sha256")
    h.update(url.encode())
    key = h.hexdigest()[:8]
    base_dir = external_files.compute_local_file_dir(DOMAIN)
    return base_dir / key


def download_firmware(config):
    url = config[CONF_URL]
    path = _compute_local_file_path(url)
    external_files.download_content(url, path)

    try:
        with open(path, "r+b") as f:
            firmware_bin = f.read()
    except FileNotFoundError as e:
        raise cv.Invalid(f"Could not open firmware file {path}: {e}") from e

    firmware_bin_md5 = hashlib.md5(firmware_bin).hexdigest()
    if firmware_bin_md5 != config[CONF_MD5]:
        raise cv.Invalid(f"{CONF_MD5} is not consistent with file contents")

    return config

def _tuning_schema():
    """Build the optional `tuning:` block: one number entity per PP/AGC parameter."""
    schema = {}
    for key, (_resid, _cmd, _is_int, _mn, _mx, _st, _default, icon, _desc) in TUNING_PARAMS.items():
        schema[cv.Optional(key)] = number.number_schema(
            XVF3800ParamNumber,
            icon=icon,
            entity_category="config",
        ).extend(
            {
                cv.Optional(CONF_INITIAL_VALUE): cv.float_,
            }
        )
    return cv.Schema(schema)


# Define the configuration schema for the component
CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(RespeakerXVF3800),
    cv.Optional(CONF_TUNING): _tuning_schema(),
    cv.Optional(CONF_MUTE_SWITCH): switch.switch_schema(
        MuteSwitch,
        icon="mdi:microphone-off",
    ).extend(cv.polling_component_schema("1s")),
    cv.Optional(CONF_DFU_VERSION): text_sensor.text_sensor_schema(
        DFUVersionTextSensor,
        icon="mdi:chip",
    ).extend(cv.polling_component_schema("30s")),
    cv.Optional(CONF_LED_BEAM_SENSOR): sensor.sensor_schema(
        LEDBeamSensor,
        icon="mdi:led-on",
        accuracy_decimals=0,
        unit_of_measurement="",
    ).extend(cv.polling_component_schema("100ms")),
    cv.GenerateID(CONF_RAW_DATA_ID): cv.declare_id(cg.uint8),
    cv.Optional(CONF_FIRMWARE): cv.All(
                {
                    cv.Required(CONF_URL): cv.url,
                    cv.Required(CONF_VERSION): cv.version_number,
                    cv.Required(CONF_MD5): cv.All(cv.string, cv.Length(min=32, max=32)),
                    cv.Optional(CONF_ON_BEGIN): automation.validate_automation(
                        {
                            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(
                                DFUStartTrigger
                            ),
                        }
                    ),
                    cv.Optional(CONF_ON_END): automation.validate_automation(
                        {
                            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(
                                DFUEndTrigger
                            ),
                        }
                    ),
                    cv.Optional(CONF_ON_ERROR): automation.validate_automation(
                        {
                            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(
                                DFUErrorTrigger
                            ),
                        }
                    ),
                    cv.Optional(CONF_ON_PROGRESS): automation.validate_automation(
                        {
                            cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(
                                DFUProgressTrigger
                            ),
                        }
                    ),
                },
                download_firmware,
            ),
}).extend(cv.COMPONENT_SCHEMA).extend(i2c.i2c_device_schema(0x2C))


OTA_RESPEAKER_XVF3800_FLASH_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RespeakerXVF3800),
    }
)


@automation.register_action(
    "respeaker_xvf3800.flash",
    RespeakerXVF3800FlashAction,
    OTA_RESPEAKER_XVF3800_FLASH_ACTION_SCHEMA,
    synchronous=False,
)
async def respeaker_xxvf3800_flash_action_to_code(config, action_id, template_arg, args):
    paren = await cg.get_variable(config[CONF_ID])
    var = cg.new_Pvariable(action_id, template_arg, paren)

    return var

# This function is called by ESPHome to generate the C++ code for the component
async def to_code(config):
    # Create the main hub component
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)
        
    # Set up mute switch if configured
    if CONF_MUTE_SWITCH in config:
        mute_switch = cg.new_Pvariable(config[CONF_MUTE_SWITCH][CONF_ID])
        await cg.register_component(mute_switch, config[CONF_MUTE_SWITCH])
        await switch.register_switch(mute_switch, config[CONF_MUTE_SWITCH])
        cg.add(var.set_mute_switch(mute_switch))
        cg.add(mute_switch.set_parent(var))
        
    # Set up DFU version sensor if configured
    if CONF_DFU_VERSION in config:
        dfu_sensor = cg.new_Pvariable(config[CONF_DFU_VERSION][CONF_ID])
        await cg.register_component(dfu_sensor, config[CONF_DFU_VERSION])
        await text_sensor.register_text_sensor(dfu_sensor, config[CONF_DFU_VERSION])
        cg.add(var.set_dfu_version_sensor(dfu_sensor))
        cg.add(dfu_sensor.set_parent(var))

    # Set up LED beam sensor if configured
    if CONF_LED_BEAM_SENSOR in config:
        led_beam_sensor = cg.new_Pvariable(config[CONF_LED_BEAM_SENSOR][CONF_ID])
        await cg.register_component(led_beam_sensor, config[CONF_LED_BEAM_SENSOR])
        await sensor.register_sensor(led_beam_sensor, config[CONF_LED_BEAM_SENSOR])
        cg.add(var.set_led_beam_sensor(led_beam_sensor))
        cg.add(led_beam_sensor.set_parent(var))

    # Set up noise-suppression / AGC tuning numbers if configured
    tuning = config.get(CONF_TUNING, {})
    for key, (resid, cmd, is_int, mn, mx, st, _default, _icon, _desc) in TUNING_PARAMS.items():
        if key not in tuning:
            continue
        conf = tuning[key]
        num = await number.new_number(conf, min_value=mn, max_value=mx, step=st)
        await cg.register_component(num, conf)
        cg.add(num.set_parent(var))
        cg.add(num.set_command(resid, cmd, is_int))
        if CONF_INITIAL_VALUE in conf:
            cg.add(num.set_initial_value(conf[CONF_INITIAL_VALUE]))
        cg.add(var.add_tuning_number(num))

    if config_fw := config.get(CONF_FIRMWARE):
        firmware_version = config_fw[CONF_VERSION].split(".")
        path = _compute_local_file_path(config_fw[CONF_URL])

        try:
            with open(path, "r+b") as f:
                firmware_bin = f.read()
        except FileNotFoundError as e:
            raise core.EsphomeError(f"Could not open firmware file {path}: {e}")

        # Convert retrieved binary file to an array of ints
        rhs = [HexInt(x) for x in firmware_bin]
        # Create an array which will reside in program memory and set the pointer to it
        firmware_bin_arr = cg.progmem_array(config[CONF_RAW_DATA_ID], rhs)
        cg.add(var.set_firmware_bin(firmware_bin_arr, len(rhs)))
        cg.add(
            var.set_firmware_version(
                int(firmware_version[0]),
                int(firmware_version[1]),
                int(firmware_version[2]),
            )
        )

        use_state_callback = False
        for conf in config_fw.get(CONF_ON_BEGIN, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, [], conf)
            use_state_callback = True
        for conf in config_fw.get(CONF_ON_PROGRESS, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, [(float, "x")], conf)
            use_state_callback = True
        for conf in config_fw.get(CONF_ON_END, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, [], conf)
            use_state_callback = True
        for conf in config_fw.get(CONF_ON_ERROR, []):
            trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
            await automation.build_automation(trigger, [(cg.uint8, "x")], conf)
            use_state_callback = True
        if use_state_callback:
            cg.add_define("USE_RESPEAKER_XVF3800_STATE_CALLBACK")
