"""Support for Xiaomi switches."""
import logging

from homeassistant.const import *
from homeassistant.components.switch import (
    DOMAIN as ENTITY_DOMAIN,
    SwitchEntity,
    DEVICE_CLASS_SWITCH,
    DEVICE_CLASS_OUTLET,
)

from . import (
    DOMAIN,
    CONF_MODEL,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    MiioDevice,
    MiotDevice,
    MiioEntity,
    MiotToggleEntity,
    ToggleSubEntity,
    bind_services_to_entries,
)
from .core.miot_spec import (
    MiotSpec,
    MiotService,
    MiotProperty,
)
from .fan import MiotWasherSubEntity

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = hass.data[DOMAIN]['configs'].get(config_entry.entry_id, dict(config_entry.data))
    await async_setup_platform(hass, config, async_add_entities)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    config.setdefault('add_entities', {})
    config['add_entities'][ENTITY_DOMAIN] = async_add_entities
    model = str(config.get(CONF_MODEL) or '')
    miot = config.get('miot_type')
    entities = []
    if model in ['pwzn.relay.banana']:
        entities.append(PwznRelaySwitchEntity(config))
    elif miot:
        spec = await MiotSpec.async_from_type(hass, miot)
        for srv in spec.get_services(
            ENTITY_DOMAIN, 'outlet', 'washer', 'pet_drinking_fountain',
        ):
            if not srv.get_property('on'):
                continue
            cfg = {
                **config,
                'name': f"{config.get('name')} {srv.description}"
            }
            entities.append(MiotSwitchEntity(cfg, srv))
    for entity in entities:
        hass.data[DOMAIN]['entities'][entity.unique_id] = entity
    async_add_entities(entities, update_before_add=True)
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class MiotSwitchEntity(MiotToggleEntity, SwitchEntity):
    def __init__(self, config: dict, miot_service: MiotService):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]

        mapping = miot_service.spec.services_mapping() or {}
        _LOGGER.info('Initializing %s (%s, token %s...), miot mapping: %s', name, host, token[:5], mapping)

        self._device = MiotDevice(mapping, host, token)
        super().__init__(name, self._device, miot_service, config=config)
        self._add_entities = config.get('add_entities') or {}

        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._subs = {}

    @property
    def device_class(self):
        typ = f'{self._model} {self._miot_service.spec.type}'
        if typ.find('outlet') >= 0:
            return DEVICE_CLASS_OUTLET
        return DEVICE_CLASS_SWITCH

    @property
    def icon(self):
        if self._miot_service.name in ['washer']:
            return 'mdi:washing-machine'
        if self._miot_service.name in ['pet_drinking_fountain']:
            return 'mdi:fountain'
        return super().icon

    async def async_update(self):
        await super().async_update()
        if self._available:
            if self._miot_service.name in ['washer', 'pet_drinking_fountain']:
                add_fans = self._add_entities.get('fan')
                pls = self._miot_service.get_properties(
                    'mode', 'spin_speed', 'drying_level',
                    'target_temperature', 'target_water_level',
                )
                for p in pls:
                    if not p.value_list or len(p.value_list) <= 1:
                        continue
                    if p.name in self._subs:
                        self._subs[p.name].update()
                    elif add_fans:
                        self._subs[p.name] = MiotWasherSubEntity(self, p)
                        add_fans([self._subs[p.name]])

                add_switches = self._add_entities.get(ENTITY_DOMAIN)
                if self._miot_service.get_action('start_wash', 'pause'):
                    pnm = 'action'
                    prop = self._miot_service.get_property('status')
                    if pnm in self._subs:
                        self._subs[pnm].update()
                    elif add_switches and prop:
                        self._subs[pnm] = MiotWasherActionSubEntity(self, prop)
                        add_switches([self._subs[pnm]])


class SwitchSubEntity(ToggleSubEntity, SwitchEntity):
    def update(self):
        super().update()


class MiotSwitchSubEntity(SwitchSubEntity):
    def __init__(self, parent, miot_property: MiotProperty, option=None):
        super().__init__(parent, miot_property.full_name, option)
        self._name = self.format_name_by_property(miot_property)
        self._miot_service = miot_property.service
        self._miot_property = miot_property
        self._prop_power = self._miot_service.get_property('on', 'power')
        if self._prop_power:
            self._option['keys'] = [*(self._option.get('keys') or []), self._prop_power.full_name]

    @property
    def is_on(self):
        if self._prop_power and self._miot_service.name in ['air_conditioner']:
            self._state = self._state and self._prop_power.from_dict(self._state_attrs)
        return self._state

    def set_parent_property(self, val):
        ret = self.call_parent('set_property', self._miot_property.full_name, val)
        if ret:
            self.update_attrs({
                self._attr: val,
            })
        return ret

    def turn_on(self, **kwargs):
        return self.set_parent_property(True)

    def turn_off(self, **kwargs):
        return self.set_parent_property(False)


class MiotWasherActionSubEntity(SwitchSubEntity):
    def __init__(self, parent, miot_property: MiotProperty, option=None):
        super().__init__(parent, miot_property.full_name, option)
        self._miot_property = miot_property
        self._miot_service = miot_property.service
        self._values_on = miot_property.list_search('Busy', 'Delay')
        self._values_off = miot_property.list_search('Off', 'Idle', 'Pause', 'Paused', 'Completed', 'Fault')

    def update(self):
        super().update()
        if self._available:
            sta = self._state_attrs.get(self._attr)
            self._state = sta not in self._values_off

    def turn_on(self, **kwargs):
        val = self._values_on[0] if self._values_on else None
        return self.miot_action('start_wash', val)

    def turn_off(self, **kwargs):
        val = self._values_off[0] if self._values_off else None
        return self.miot_action(['pause', 'stop_washing'], val)

    def miot_action(self, act, sta=None):
        ret = False
        if not isinstance(act, list):
            act = [act]
        act = self._miot_service.get_action(*act)
        if act:
            ret = self.call_parent('miot_action', self._miot_service.iid, act.iid)
            if ret and sta is not None:
                self.update_attrs({
                    self._attr: sta,
                })
        return ret

    @property
    def icon(self):
        return 'mdi:play-box'


class MiotCookerSwitchSubEntity(SwitchSubEntity):
    def __init__(self, parent, miot_property: MiotProperty, option=None):
        super().__init__(parent, miot_property.full_name, option)

    @property
    def is_on(self):
        return self._parent.is_on


class PwznRelaySwitchEntity(MiioEntity, SwitchEntity):
    def __init__(self, config: dict):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]
        _LOGGER.info('Initializing %s with host %s (token %s...)', name, host, token[:5])

        self._config = config
        self._device = MiioDevice(host, token)
        super().__init__(name, self._device)
        self._add_entities = config.get('add_entities') or {}
        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._success_result = [0]

        self._props = [
            'relay_names_g1', 'relay_status_g1',
            'relay_names_g2', 'relay_status_g2',
            'g2Enable', 'codeEnable',
        ]
        self._subs = {}

    @property
    def device_class(self):
        return DEVICE_CLASS_SWITCH

    async def async_update(self):
        await super().async_update()
        await self.hass.async_add_executor_job(self.update_all)

    def update_all(self):
        if self._available:
            attrs = self._state_attrs
            self._state = False
            add_switches = self._add_entities.get(ENTITY_DOMAIN)
            idx = 0
            for g in [1, 2]:
                if f'relay_status_g{g}' not in attrs:
                    continue
                sta = int(attrs.get(f'relay_status_g{g}') or 0)
                if sta:
                    self._state = True
                nms = str(attrs.get(f'relay_names_g{g}') or '').split('-')
                s = 0
                b = 1
                for n in nms:
                    s += 1
                    k = f'g{g}s{s}'
                    self._state_attrs[k] = STATE_ON if sta & b else STATE_OFF
                    if k in self._subs:
                        self._subs[k].update()
                    elif add_switches:
                        self._subs[k] = PwznRelaySwitchSubEntity(self, g, s, {
                            'attr': k,
                            'index': idx,
                        })
                        add_switches([self._subs[k]])
                    b <<= 1
                    idx += 1

            if self.custom_config('enable_advanced'):
                for k in ['g2Enable', 'codeEnable']:
                    if k not in attrs:
                        continue
                    self._state_attrs[k] = STATE_ON if attrs[k] else STATE_OFF
                    if k in self._subs:
                        self._subs[k].update()
                    elif add_switches:
                        self._subs[k] = PwznRelaySwitchSubEntity(self, 0, 0, {
                            'attr': k,
                        })
                        add_switches([self._subs[k]])

    def turn_on(self, **kwargs):
        ret = self.send_command('power_all', [1])
        if ret:
            full = (1 << 16) - 1
            self.update_attrs({
                'relay_status_g1': full,
                'relay_status_g2': full,
            }, update_parent=False)
            self.update_all()
            self._state = True
        return ret

    def turn_off(self, **kwargs):
        ret = self.send_command('power_all', [0])
        if ret:
            self.update_attrs({
                'relay_status_g1': 0,
                'relay_status_g2': 0,
            }, update_parent=False)
            self.update_all()
            self._state = False
        return ret


class PwznRelaySwitchSubEntity(SwitchSubEntity):
    def __init__(self, parent: PwznRelaySwitchEntity, group, switch, option=None):
        self._group = group
        self._switch = switch
        self._switch_index = 0
        key = f'g{group}s{switch}'
        if isinstance(option, dict):
            if option.get('attr'):
                key = option.get('attr')
            self._switch_index = int(option.get('index') or 0)
        super().__init__(parent, key, option)

    def turn_parent(self, on):
        if self._attr == 'g2Enable':
            ret = self.call_parent('send_command', 'set_g2enable', [1 if on else 0])
        elif self._attr == 'codeEnable':
            ret = self.call_parent('send_command', 'set_codeEnable', [1 if on else 0])
        else:
            ret = self.call_parent('send_command', 'power_on' if on else 'power_off', [self._switch_index])
        if ret:
            self.update_attrs({
                self._attr: STATE_ON if on else STATE_OFF
            }, update_parent=True)
            self._state = on and True
        return ret

    def turn_on(self, **kwargs):
        return self.turn_parent(True)

    def turn_off(self, **kwargs):
        return self.turn_parent(False)
