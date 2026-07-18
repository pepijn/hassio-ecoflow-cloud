import logging
import time
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity

from custom_components.ecoflow_cloud.devices import BaseDevice, const
from custom_components.ecoflow_cloud.devices.data_holder import PreparedData
from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.number import (
    ChargingPowerEntity,
    MaxBatteryLevelEntity,
    MinBatteryLevelEntity,
)
from custom_components.ecoflow_cloud.select import DictSelectEntity, TimeoutDictSelectEntity
from custom_components.ecoflow_cloud.sensor import (
    # AmpSensorEntity,
    CapacitySensorEntity,
    # CyclesSensorEntity,
    # InAmpSensorEntity,
    # InVoltSensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    # OutVoltSensorEntity,
    OutWattsSensorEntity,
    QuotaScheduledStatusSensorEntity,
    RemainSensorEntity,
    TempSensorEntity,
    # VoltSensorEntity,
)
import jsonpath_ng.ext as jp

from custom_components.ecoflow_cloud.switch import BeeperEntity, EnabledEntity

_LOGGER = logging.getLogger(__name__)


class OutputEnabledEntity(EnabledEntity):
    """Public-API output switch whose live state comes from a flowInfo_* key.

    The Delta Pro 3 public quota reports each output's state only in flowInfo*
    ({0: off, 2: on}) and never sends the cfg_*_out_open flag a switch would
    normally read, so the switch sat at "unknown". Read state from ``flow_key``
    (enableValue=2 -> on) while keeping the historical cfg key as the mqtt_key,
    so the entity id and the set-command payload are unchanged.
    """

    # After a toggle the EcoFlow cloud keeps reporting the OLD flowInfo_* value
    # for a few seconds, which would briefly flip the switch back. Hold the
    # commanded state until the reported value agrees (or this window elapses).
    _HOLD_SECONDS = 20

    def __init__(self, client, device, cfg_key: str, flow_key: str, title: str, command):
        super().__init__(client, device, cfg_key, title, command, enableValue=2, disableValue=0)
        # read on/off state from the flowInfo_* key rather than the cfg_* flag
        self._mqtt_key_expr = jp.parse(self._adopt_json_key(flow_key))
        self._hold_state: bool | None = None
        self._hold_until: float = 0.0

    def _begin_hold(self, state: bool) -> None:
        self._hold_state = state
        self._hold_until = time.monotonic() + self._HOLD_SECONDS
        self._attr_is_on = state
        self.schedule_update_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        super().turn_on(**kwargs)
        self._begin_hold(True)

    def turn_off(self, **kwargs: Any) -> None:
        super().turn_off(**kwargs)
        self._begin_hold(False)

    def _update_value(self, val: Any) -> bool:
        if self._hold_state is not None and time.monotonic() < self._hold_until:
            if (val == 2) == self._hold_state:
                self._hold_state = None  # reported state caught up; release hold
            else:
                self._attr_is_on = self._hold_state  # ignore lagged value
                return True
        else:
            self._hold_state = None
        return super()._update_value(val)


class DeltaPro3(BaseDevice):
    # The device's SetReply echoes the cfg_*_out_open flag it applied; map it to
    # the flowInfo_* key the output switches read so a toggle updates the switch
    # immediately. (The live /quota that would carry flowInfo* is only pushed as
    # flat BMS reports with no "params" wrapper, so the switch would otherwise
    # revert to its stale startup value.)
    _CFG_TO_FLOW: dict[str, str] = {
        "cfgDc12vOutOpen": "flowInfo12v",
        "cfgHvAcOutOpen": "flowInfoAcHvOut",
        "cfgLvAcOutOpen": "flowInfoAcLvOut",
    }

    def _set_cmd(self, params: dict[str, Any]) -> dict[str, Any]:
        """Build a public-API set command and log it for toggle verification.

        needAck + boolean values match the envelope proven to control the DP3
        (the earlier int values without needAck were silently ignored by the
        device — no set_reply, no state change).
        """
        cmd = {
            "sn": self.device_info.sn,
            "cmdId": 17,
            "dirDest": 1,
            "dirSrc": 1,
            "cmdFunc": 254,
            "dest": 2,
            "needAck": True,
            "params": params,
        }
        _LOGGER.info("[DeltaPro3] sending set command: %s", cmd)
        return cmd

    def _prepare_data_set_reply_topic(self, raw_data: bytes) -> PreparedData:
        prepared = super()._prepare_data_set_reply_topic(raw_data)
        reply = prepared.raw_data
        _LOGGER.info("[DeltaPro3] set_reply: %s", reply)
        # The SetReply confirms the applied output state; reflect it immediately
        # so the switch doesn't revert to a stale flowInfo_* value.
        data = reply.get("data") if isinstance(reply, dict) else None
        if isinstance(data, dict) and data.get("configOk"):
            update = {
                flow_key: (2 if data[cfg_key] else 0)
                for cfg_key, flow_key in self._CFG_TO_FLOW.items()
                if cfg_key in data
            }
            if update:
                _LOGGER.info("[DeltaPro3] applying set_reply state -> %s", update)
                return PreparedData(None, {"params": update}, reply)
        return prepared

    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        return [
            LevelSensorEntity(client, self, "bmsBattSoc", const.MAIN_BATTERY_LEVEL),
            # .attr("bmsDesignCap", const.ATTR_DESIGN_CAPACITY, 0)
            # .attr("bmsFullCapMah", const.ATTR_FULL_CAPACITY, 0)
            # .attr("bmsRemainCapMah", const.ATTR_REMAIN_CAPACITY, 0),
            CapacitySensorEntity(client, self, "bmsDesignCap", const.MAIN_DESIGN_CAPACITY, False),
            # CapacitySensorEntity(client, self, "bmsFullCapMah", const.MAIN_FULL_CAPACITY, False),
            # CapacitySensorEntity(client, self, "bmsRemainCapMah", const.MAIN_REMAIN_CAPACITY, False),
            LevelSensorEntity(client, self, "cmsChgDsgState", "Charging/Discharging State", False),
            LevelSensorEntity(client, self, "cmsBmsRunState", "BMS Run State", False),
            LevelSensorEntity(client, self, "cmsBattSoc", const.COMBINED_BATTERY_LEVEL),
            # LevelSensorEntity(client, self, "bmsBattSoh", const.SOH),
            # CyclesSensorEntity(client, self, "bmsCycles", const.CYCLES),
            # VoltSensorEntity(client, self, "bmsBattVol", const.BATTERY_VOLT, False)
            # .attr("bmsMinCellVol", const.ATTR_MIN_CELL_VOLT, 0)
            # .attr("bmsMaxCellVol", const.ATTR_MAX_CELL_VOLT, 0),
            # VoltSensorEntity(client, self, "bmsMinCellVol", const.MIN_CELL_VOLT, False),
            # VoltSensorEntity(client, self, "bmsMaxCellVol", const.MAX_CELL_VOLT, False),
            # AmpSensorEntity(client, self, "bmsBattAmp", const.MAIN_BATTERY_CURRENT, False),
            TempSensorEntity(client, self, "bmsMaxCellTemp", const.MAX_CELL_TEMP, False),
            TempSensorEntity(client, self, "bmsMinCellTemp", const.MIN_CELL_TEMP, False),
            # TempSensorEntity(client, self, "bmsMaxMosTemp", const.BATTERY_TEMP)
            # .attr("bmsMinCellTemp", const.ATTR_MIN_CELL_TEMP, 0)
            # .attr("bmsMaxCellTemp", const.ATTR_MAX_CELL_TEMP, 0),
            RemainSensorEntity(client, self, "bmsChgRemTime", const.CHARGE_REMAINING_TIME),
            RemainSensorEntity(client, self, "bmsDsgRemTime", const.DISCHARGE_REMAINING_TIME),
            RemainSensorEntity(client, self, "cmsChgRemTime", "Total Charging Time"),
            RemainSensorEntity(client, self, "cmsDsgRemTime", "Total Discharging Time"),
            InWattsSensorEntity(client, self, "powInSumW", const.TOTAL_IN_POWER).with_energy(),
            OutWattsSensorEntity(client, self, "powOutSumW", const.TOTAL_OUT_POWER).with_energy(),
            InWattsSensorEntity(client, self, "powGetAcIn", const.AC_IN_POWER),
            OutWattsSensorEntity(client, self, "powGetAcHvOut", "Real-time grid power"),
            OutWattsSensorEntity(client, self, "powGetAc", const.AC_OUT_POWER),
            OutWattsSensorEntity(client, self, "powGet12v", "12V DC Output Power"),
            OutWattsSensorEntity(client, self, "powGet24v", "24V DC Output Power"),
            OutWattsSensorEntity(client, self, "powGetAcLvOut", "Real-time low-voltage AC output power"),
            OutWattsSensorEntity(
                client, self, "powGetAcLvTt30Out", "Real-time power of the low-voltage AC output port"
            ),
            # OutVoltSensorEntity(client, self, "powGet12vVol", "12V DC Output Voltage"),
            # OutVoltSensorEntity(client, self, "powGet24vVol", "24V DC Output Voltage"),
            InWattsSensorEntity(client, self, "powGetPvH", "Solar High Voltage Input Power"),
            InWattsSensorEntity(client, self, "powGetPvL", "Solar Low Voltage Input Power"),
            # InVoltSensorEntity(client, self, "powGetPvHVol", "Solar HV Input Voltage"),
            # InVoltSensorEntity(client, self, "powGetPvLVol", "Solar LV Input Voltage"),
            # InAmpSensorEntity(client, self, "powGetPvHAmp", "Solar HV Input Current"),
            # InAmpSensorEntity(client, self, "powGetPvLAmp", "Solar LV Input Current"),
            OutWattsSensorEntity(client, self, "powGetQcusb1", const.USB_QC_1_OUT_POWER),
            OutWattsSensorEntity(client, self, "powGetQcusb2", const.USB_QC_2_OUT_POWER),
            OutWattsSensorEntity(client, self, "powGetTypec1", const.TYPEC_1_OUT_POWER),
            OutWattsSensorEntity(client, self, "powGetTypec2", const.TYPEC_2_OUT_POWER),
            OutWattsSensorEntity(client, self, "powGet5p8", "5P8 Power I/O Port Power"),
            OutWattsSensorEntity(client, self, "powGet4p81", "4P8 Extra Battery Port 1 Power", False, True),
            OutWattsSensorEntity(client, self, "powGet4p82", "4P8 Extra Battery Port 2 Power", False, True),
            # OutWattsSensorEntity(client, self, "acOutFreq", "AC Output Frequency"),
            LevelSensorEntity(client, self, "plugInInfoAcInFeq", "AC Input Frequency"),
            # Poll the full state every 60s (like PowerStream / Delta 3 Max Plus)
            # so output changes made on the device or app show up automatically —
            # the DP3 does not push output on/off state live over MQTT.
            QuotaScheduledStatusSensorEntity(client, self, 60),
        ]

    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        return [
            MaxBatteryLevelEntity(
                client,
                self,
                "cmsMaxChgSoc",
                const.MAX_CHARGE_LEVEL,
                50,
                100,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgMaxChgSoc": value},
                },
            ),
            MinBatteryLevelEntity(
                client,
                self,
                "cmsMinDsgSoc",
                const.MIN_DISCHARGE_LEVEL,
                0,
                30,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgMinDsgSoc": value},
                },
            ),
            MaxBatteryLevelEntity(
                client,
                self,
                "cmsOilOnSoc",
                "Smart Generator Start SOC",
                0,
                100,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgCmsOilOnSoc": value},
                },
            ),
            MinBatteryLevelEntity(
                client,
                self,
                "cmsOilOffSoc",
                "Smart Generator Stop SOC",
                0,
                100,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgCmsOilOffSoc": value},
                },
            ),
            ChargingPowerEntity(
                client,
                self,
                "cfgPlugInInfoAcInChgPowMax",
                const.AC_CHARGING_POWER,
                400,
                2900,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgPlugInInfoAcInChgPowMax": value},
                },
            ),
        ]

    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        return [
            BeeperEntity(
                client,
                self,
                "enBeep",
                const.BEEPER,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgBeepEn": value},
                },
            ),
            OutputEnabledEntity(
                client,
                self,
                "cfgHvAcOutOpen",
                "flowInfoAcHvOut",
                "AC HV Output Enabled",
                lambda value, params=None: self._set_cmd({"cfgHvAcOutOpen": value == 2}),
            ),
            OutputEnabledEntity(
                client,
                self,
                "cfgLvAcOutOpen",
                "flowInfoAcLvOut",
                "AC LV Output Enabled",
                lambda value, params=None: self._set_cmd({"cfgLvAcOutOpen": value == 2}),
            ),
            OutputEnabledEntity(
                client,
                self,
                "cfgDc12vOutOpen",
                "flowInfo12v",
                "12V DC Output Enabled",
                lambda value, params=None: self._set_cmd({"cfgDc12vOutOpen": value == 2}),
            ),
            EnabledEntity(
                client,
                self,
                "xboostEn",
                const.XBOOST_ENABLED,
                lambda value, params=None: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgXboostEn": value},
                },
            ),
            EnabledEntity(
                client,
                self,
                "acEnergySavingOpen",
                "AC Energy Saving Enabled",
                lambda value, params=None: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgAcEnergySavingOpen": value},
                },
            ),
            EnabledEntity(
                client,
                self,
                "cmsOilSelfStart",
                "Smart Generator Auto Start/Stop",
                lambda value, params=None: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgCmsOilSelfStart": value},
                },
            ),
        ]

    def selects(self, client: EcoflowApiClient) -> list[SelectEntity]:
        return [
            TimeoutDictSelectEntity(
                client,
                self,
                "screenOffTime",
                const.SCREEN_TIMEOUT,
                const.SCREEN_TIMEOUT_OPTIONS,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgScreenOffTime": value},
                },
            ),
            TimeoutDictSelectEntity(
                client,
                self,
                "acStandbyTime",
                const.AC_TIMEOUT,
                const.AC_TIMEOUT_OPTIONS,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgAcStandbyTime": value},
                },
            ),
            TimeoutDictSelectEntity(
                client,
                self,
                "dcStandbyTime",
                "DC Timeout",
                const.UNIT_TIMEOUT_OPTIONS_LIMITED,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgDcStandbyTime": value},
                },
            ),
            TimeoutDictSelectEntity(
                client,
                self,
                "bleStandbyTime",
                "Bluetooth Timeout",
                const.UNIT_TIMEOUT_OPTIONS_LIMITED,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgBleStandbyTime": value},
                },
            ),
            TimeoutDictSelectEntity(
                client,
                self,
                "devStandbyTime",
                "Device Timeout",
                const.UNIT_TIMEOUT_OPTIONS_LIMITED,
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgDevStandbyTime": value},
                },
            ),
            DictSelectEntity(
                client,
                self,
                "plugInInfoAcOutType",
                "AC Output Type",
                {"HV+LV": 0, "HV Only": 1, "LV Only": 2},
                lambda value: {
                    "sn": self.device_info.sn,
                    "cmdId": 17,
                    "dirDest": 1,
                    "dirSrc": 1,
                    "cmdFunc": 254,
                    "dest": 2,
                    "params": {"cfgPlugInInfoAcOutType": int(value)},
                },
            ),
        ]
