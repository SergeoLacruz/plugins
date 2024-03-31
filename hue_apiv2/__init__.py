#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2020-      Martin Sinn                         m.sinn@gmx.de
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  hue_apiv2 plugin to run with SmartHomeNG
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import qhue
import requests
import xmltodict

# new for asyncio -->
import threading
import asyncio
from concurrent.futures import CancelledError
import time

from aiohue import HueBridgeV2
# <-- new for asyncio

# for hostname retrieval for registering with the bridge
from socket import getfqdn

from lib.model.smartplugin import *
from lib.item import Items

from .webif import WebInterface

from .discover_bridges import discover_bridges

# If a needed package is imported, which might be not installed in the Python environment,
# add it to a requirements.txt file within the plugin's directory

mapping_delimiter = '|'


class HueApiV2(SmartPlugin):
    """
    Main class of the Plugin. Does all plugin specific stuff and provides
    the update functions for the items
    """

    PLUGIN_VERSION = '0.2.0'    # (must match the version specified in plugin.yaml)

    hue_sensor_state_values          = ['daylight', 'temperature', 'presence', 'lightlevel', 'status']
    hue_sensor_config_values         = ['reachable', 'battery', 'on', 'sunriseoffset', 'sunsetoffset']

    br = None               # Bridge object for communication with the bridge
    bridge_config = {}
    bridge_scenes = {}
    bridge_sensors = {}

    v2bridge = None
    devices = {}    # devices connected to the hue bridge


    def __init__(self, sh):
        """
        Initalizes the plugin.

        If you need the sh object at all, use the method self.get_sh() to get it. There should be almost no need for
        a reference to the sh object any more.

        Plugins have to use the new way of getting parameter values:
        use the SmartPlugin method get_parameter_value(parameter_name). Anywhere within the Plugin you can get
        the configured (and checked) value for a parameter by calling self.get_parameter_value(parameter_name). It
        returns the value in the datatype that is defined in the metadata.
        """

        # Call init code of parent class (SmartPlugin)
        super().__init__()

        # get the parameters for the plugin (as defined in metadata plugin.yaml):
        #self.bridge_type = self.get_parameter_value('bridge_type')
        self.bridge_serial = self.get_parameter_value('bridge_serial')
        self.bridge_ip = self.get_parameter_value('bridge_ip')
        self.bridge_port = self.get_parameter_value('bridge_port')
        self.bridge_user = self.get_parameter_value('bridge_user')

        # polled for value changes by adding a scheduler entry in the run method of this plugin
        self.sensor_items_configured = False   # If no sensor items are configured, the sensor-scheduler is not started
        self._default_transition_time = int(float(self.get_parameter_value('default_transitionTime'))*1000)

        self.discovered_bridges = []
        self.bridge = self.get_bridge_desciption(self.bridge_ip, self.bridge_port)
        if False and self.bridge == {}:
            # discover hue bridges on the network
            self.discovered_bridges = self.discover_bridges()

            # self.bridge = self.get_parameter_value('bridge')
            # self.get_bridgeinfo()
            # self.logger.warning("Configured Bridge={}, type={}".format(self.bridge, type(self.bridge)))

            if self.bridge_serial == '':
                self.bridge = {}
            else:
                # if a bridge is configured
                # find bridge using its serial number
                self.bridge = self.get_data_from_discovered_bridges(self.bridge_serial)
                if self.bridge.get('serialNumber', '') == '':
                    self.logger.warning("Configured bridge {} is not in the list of discovered bridges, starting second discovery")
                    self.discovered_bridges = self.discover_bridges()

                    if self.bridge.get('serialNumber', '') == '':
                        # if not discovered, use stored ip address
                        self.bridge['ip'] = self.bridge_ip
                        self.bridge['port'] = self.bridge_port
                        self.bridge['serialNumber'] = self.bridge_serial
                        self.logger.warning("Configured bridge {} is still not in the list of discovered bridges, trying with stored ip address {}:{}".format(self.bridge_serial, self.bridge_ip, self.bridge_port))

                        api_config = self.get_api_config_of_bridge('http://'+self.bridge['ip']+':'+str(self.bridge['port'])+'/')
                        self.bridge['datastoreversion'] = api_config.get('datastoreversion', '')
                        self.bridge['apiversion'] = api_config.get('apiversion', '')
                        self.bridge['swversion'] = api_config.get('swversion', '')
                        self.bridge['modelid'] = api_config.get('modelid', '')


        self.bridge['username'] = self.bridge_user
        if self.bridge.get('ip', '') != self.bridge_ip:
            # if ip address of bridge has changed, store new ip address in configuration data
            self.update_plugin_config()

        # dict to store information about items handled by this plugin
        self.plugin_items = {}

        self.init_webinterface(WebInterface)

        return


    # ----------------------------------------------------------------------------------

    def run(self):
        """
        Run method for the plugin
        """
        self.logger.debug("Run method called")

        # Start the asyncio eventloop in it's own thread
        # and set self.alive to True when the eventloop is running
        self.start_asyncio(self.plugin_coro())

        # self.alive = True     # if using asyncio, do not set self.alive here. Set it in the session coroutine

#        while not self.alive:
#            pass
#        self.run_asyncio_coro(self.list_asyncio_tasks())

        return


    def stop(self):
        """
        Stop method for the plugin
        """
        self.logger.debug("Stop method called")

        # self.alive = False     # if using asyncio, do not set self.alive here. Set it in the session coroutine

        # Stop the asyncio eventloop and it's thread
        self.stop_asyncio()
        return


    # ----------------------------------------------------------------------------------

    async def plugin_coro(self):
        """
        Coroutine for the session that communicates with the hue bridge

        This coroutine opens the session to the hue bridge and
        only terminate, when the plugin ois stopped
        """
        self.logger.notice("plugin_coro started")

        self.logger.debug("plugin_coro: Opening session")

        host = '10.0.0.190'
        appkey = 'uJLI9mXMgsPoV5g6FqKbKQwkKNdgjJmTcAv4SXYA'
        #self.v2bridge = HueBridgeV2(host, appkey)
        self.v2bridge = HueBridgeV2(self.bridge_ip, self.bridge_user)

        self.alive = True
        self.logger.info("plugin_coro: Plugin is running (self.alive=True)")

        async with self.v2bridge:
            self.logger.info(f"plugin_coro: Connected to bridge: {self.v2bridge.bridge_id}")
            self.logger.info(f" - device id: {self.v2bridge.config.bridge_device.id}")
            self.logger.info(f" - name     : {self.v2bridge.config.bridge_device.metadata.name}")

            self.unsubscribe_function = self.v2bridge.subscribe(self.handle_event)

            try:
                self.initialize_items_from_bridge()
            except Exception as ex:
                # catch exception to prevent plugin_coro from unwanted termination
                self.logger.exception(f"Exception in initialize_items_from_bridge(): {ex}")

            # block: wait until a stop command is received by the queue
            queue_item = await self.run_queue.get()

        self.alive = False
        self.logger.info("plugin_coro: Plugin is stopped (self.alive=False)")

        self.logger.debug("plugin_coro: Closing session")
        # husky2: await self.apiSession.close()
        #self.unsubscribe_function()

        self.logger.notice("plugin_coro finished")
        return


    def handle_event(self, event_type, event_item, initialize=False):
        """
        Callback function for bridge.subscribe()
        """
        if isinstance(event_type, str):
            e_type = event_type
        else:
            e_type = str(event_type.value)
        if e_type == 'update':
            if event_item.type.value == 'light':
                self.update_light_items_from_event(event_item)
            elif event_item.type.value == 'grouped_light':
                self.update_group_items_from_event(event_item)
            elif event_item.type.value == 'zigbee_connectivity':
                lights = self.v2bridge.devices.get_lights(event_item.owner.rid)
                if len(lights) > 0:
                    for light in lights:
                        mapping_root = light.id + mapping_delimiter + 'light' + mapping_delimiter
                        self.update_items_with_mapping(light, mapping_root, 'reachable', str(event_item.status.value) == 'connected', initialize)
                        self.update_items_with_mapping(light, mapping_root, 'connectivity', event_item.status.value, initialize)
                    mapping_root = event_item.id + mapping_delimiter + 'sensor' + mapping_delimiter
                    self.update_items_with_mapping(event_item, mapping_root, 'connectivity', event_item.status.value, initialize)
                    self.update_items_with_mapping(event_item, mapping_root, 'reachable', str(event_item.status.value) == 'connected', initialize)
                else:
                    mapping_root = event_item.id + mapping_delimiter + 'sensor' + mapping_delimiter
                    self.update_items_with_mapping(event_item, mapping_root, 'connectivity', event_item.status.value, initialize)
                    self.update_items_with_mapping(event_item, mapping_root, 'reachable', str(event_item.status.value) == 'connected', initialize)

                    device_name = self._get_device_name(event_item.owner.rid)
                    status = event_item.status.value
                    self.logger.notice(f"handle_event: '{event_item.type.value}' is unhandled  - device='{device_name}', {status=}  -  event={event_item}")
                    device = self._get_device(event_item.owner.rid)
                    self.logger.notice(f" - {device=}")
                    sensors = self.v2bridge.devices.get_sensors(event_item.owner.rid)
                    self.logger.notice(f" - {sensors=}")

            elif event_item.type.value == 'button':
                #device_name = self._get_device_name(event_item.owner.rid)
                #control_id = event_item.metadata.control_id
                #last_event = event_item.button.last_event.value
                #sensor_id = event_item.id
                #self.logger.notice(f"handle_event: '{event_item.type.value}' is handled  - device='{device_name}', id={control_id}, last_event={last_event}, sensor={sensor_id}  -  event={event_item}")
                self.update_button_items_from_event(event_item, initialize=initialize)
            elif event_item.type.value == 'device_power':
                self.update_devicepower_items_from_event(event_item, initialize=initialize)
            elif event_item.type.value == 'geofence_client':
                pass
            else:
                self.logger.notice(f"handle_event: Eventtype '{event_item.type.value}' is unhandled  -  event={event_item}")
        else:
            self.logger.notice(f"handle_event: Eventtype {event_type.value} is unhandled")
        return

    def _get_device(self, device_id):
        device = None
        for d in self.v2bridge.devices:
            if device_id in d.id:
                device = d
                break
        return device

    def _get_device_name(self, device_id):
        device = self._get_device(device_id)
        if device is None:
            return '-'
        return device.metadata.name

    def _get_light_name(self, light_id):
        name = '-'
        for d in self.v2bridge.devices:
            if light_id in d.lights:
                name = d.metadata.name
        return name

    def log_event(self, event_type, event_item):

        if event_item.type.value == 'geofence_client':
            pass
        elif event_item.type.value == 'light':
            mapping = event_item.id + mapping_delimiter + event_item.type.value + mapping_delimiter + 'y'
            self.logger.debug(f"handle_event: {event_type.value} {event_item.type.value}: '{self._get_light_name(event_item.id)}' {event_item.id_v1} {mapping=} {event_item.id}  -  {event_item=}")
        elif event_item.type.value == 'grouped_light':
            self.logger.notice(f"handle_event: {event_type.value} {event_item.type.value}: {event_item.id} {event_item.id_v1}  -  {event_item=}")
        else:
            self.logger.notice(f"handle_event: {event_type.value} {event_item.type.value}: {event_item.id}  -  {event_item=}")
        return


    def update_light_items_from_event(self, event_item, initialize=False):

        mapping_root = event_item.id + mapping_delimiter + event_item.type.value + mapping_delimiter

        if self.get_items_for_mapping(mapping_root + 'on') != []:
            self.logger.notice(f"update_light_items_from_event: '{self._get_light_name(event_item.id)}' - {event_item}")

        if initialize:
            self.update_items_with_mapping(event_item, mapping_root, 'name', self._get_light_name(event_item.id), initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'dict', {}, initialize)

        self.update_items_with_mapping(event_item, mapping_root, 'on', event_item.on.on, initialize)
        self.update_items_with_mapping(event_item, mapping_root, 'bri', event_item.dimming.brightness, initialize)
        self.update_items_with_mapping(event_item, mapping_root, 'xy', [event_item.color.xy.x, event_item.color.xy.y], initialize)
        try:
            mirek = event_item.color_temperature.mirek
        except:
            mirek = 0
        self.update_items_with_mapping(event_item, mapping_root, 'ct', mirek, initialize)

        return


    def update_group_items_from_event(self, event_item, initialize=False):
        if event_item.type.value == 'grouped_light':
            mapping_root = event_item.id + mapping_delimiter + 'group' + mapping_delimiter

            if self.get_items_for_mapping(mapping_root + 'on') != []:
                room = self.v2bridge.groups.grouped_light.get_zone(event_item.id)
                name = room.metadata.name
                if event_item.id_v1 == '/groups/0':
                    name = '(All lights)'
                self.logger.notice(f"update_group_items_from_event: '{name}' - {event_item}")

            if initialize:
                self.update_items_with_mapping(event_item, mapping_root, 'name', self._get_light_name(event_item.id), initialize)
                self.update_items_with_mapping(event_item, mapping_root, 'dict', {}, initialize)

            self.update_items_with_mapping(event_item, mapping_root, 'on', event_item.on.on, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'bri', event_item.dimming.brightness, initialize)

        return


    def update_button_items_from_event(self, event_item, initialize=False):

        mapping_root = event_item.id + mapping_delimiter + event_item.type.value + mapping_delimiter

        if initialize:
            self.update_items_with_mapping(event_item, mapping_root, 'name', self._get_device_name(event_item.owner.rid) )

        last_event = event_item.button.last_event.value

        #if mapping_root.startswith('2463dfc8-ee7f-4484-8901-3f5bbb319e4d'):
        #    self.logger.notice(f"update_button_items_from_event: Button1: {last_event}")

        self.update_items_with_mapping(event_item, mapping_root, 'event', last_event, initialize)
        if last_event == 'initial_press':
            self.update_items_with_mapping(event_item, mapping_root, 'initial_press', True, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'repeat', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'short_release', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'long_release', False, initialize)
        if last_event == 'repeat':
            self.update_items_with_mapping(event_item, mapping_root, 'initial_press', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'repeat', True, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'short_release', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'long_release', False, initialize)
        if last_event == 'short_release':
            self.update_items_with_mapping(event_item, mapping_root, 'initial_press', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'repeat', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'short_release', True, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'long_release', False, initialize)
        if last_event == 'long_release':
            self.update_items_with_mapping(event_item, mapping_root, 'initial_press', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'repeat', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'short_release', False, initialize)
            self.update_items_with_mapping(event_item, mapping_root, 'long_release', True, initialize)

        return


    def update_devicepower_items_from_event(self, event_item, initialize=False):

        mapping_root = event_item.id + mapping_delimiter + event_item.type.value + mapping_delimiter

        if initialize:
            self.update_items_with_mapping(event_item, mapping_root, 'name', self._get_device_name(event_item.owner.rid) )

        self.update_items_with_mapping(event_item, mapping_root, 'power_status', event_item.power_state.battery_state.value, initialize)
        self.update_items_with_mapping(event_item, mapping_root, 'battery_level', event_item.power_state.battery_level, initialize)

        return


    def update_items_with_mapping(self, event_item, mapping_root, function, value, initialize=False):

        update_items = self.get_items_for_mapping(mapping_root + function)

        for item in update_items:
            #if initialize:
            #    # set v2 id in config data
            #    config_data = self.get_item_config(item)
            #    self.logger.debug(f"update_items_with_mapping: setting config_data for id_v1={config_data['id_v1']} -> Setting id to {event_item.id}")
            #    config_data['id'] = event_item.id
            item(value, self.get_fullname())


    def initialize_items_from_bridge(self):
        """
        Initializing the item values with data from the hue bridge after connecting to in
        """
        self.logger.debug('initialize_items_from_bridge: Start')
        self.logger.notice(f"initialize_items_from_bridge: v2bridge={dir(self.v2bridge)}")
        #self.v2bridge.lights.initialize(None)
        for event_item in self.v2bridge.lights:
            self.update_light_items_from_event(event_item, initialize=True)
        for event_item in self.v2bridge.groups:
            self.update_group_items_from_event(event_item, initialize=True)
        for event_item in self.v2bridge.sensors:
            #self.update_button_items_from_event(event_item, initialize=True)
            self.handle_event('update', event_item, initialize=True)

        self.logger.debug('initialize_items_from_bridge: End')
        return



# ----------------------------------------------------------------------------------

    def parse_item(self, item):
        """
        Default plugin parse_item method. Is called when the plugin is initialized.
        The plugin can, corresponding to its attribute keywords, decide what to do with
        the item in future, like adding it to an internal array for future reference
        :param item:    The item to process.
        :return:        If the plugin needs to be informed of an items change you should return a call back function
                        like the function update_item down below. An example when this is needed is the knx plugin
                        where parse_item returns the update_item function when the attribute knx_send is found.
                        This means that when the items value is about to be updated, the call back function is called
                        with the item, caller, source and dest as arguments and in case of the knx plugin the value
                        can be sent to the knx with a knx write function within the knx plugin.
        """
        resource = self.get_iattr_value(item.conf, 'hue_apiv2_resource')
        function = self.get_iattr_value(item.conf, 'hue_apiv2_function')
        if self.has_iattr(item.conf, 'hue_apiv2_id') and self.has_iattr(item.conf, 'hue_apiv2_function') or \
           resource == 'scene' and function == 'activate_scene':
            config_data = {}
            id = self.get_iattr_value(item.conf, 'hue_apiv2_id')
            if id is None:
                id = 'None'
            config_data['id'] = id
            #config_data['id_v1'] = id
            config_data['resource'] = self.get_iattr_value(item.conf, 'hue_apiv2_resource')
            config_data['function'] = self.get_iattr_value(item.conf, 'hue_apiv2_function')
            config_data['transition_time'] = self.get_iattr_value(item.conf, 'hue_apiv2_transition_time')

            config_data['name'] = ''    # to be filled during initialization of v2bridge
            #if self.has_iattr(item.conf, 'hue_apiv2_reference_light_id'):
            #    if config_data['resource'] == "group":
            #        config_data['hue_apiv2_reference_light_id'] = self.get_iattr_value(item.conf, 'hue_apiv2_reference_light_id')

            config_data['item'] = item

#            mapping = config_data['id_v1'] + mapping_delimiter + config_data['resource'] + mapping_delimiter + config_data['function']
            mapping = config_data['id'] + mapping_delimiter + config_data['resource'] + mapping_delimiter + config_data['function']

            # updating=True, if not read only
            if not config_data['function'] in ['reachable', 'battery'] and \
               not config_data['function'] in self.hue_sensor_state_values:
                pass
#                self.add_item(item, mapping=mapping, config_data_dict=config_data, updating=True)
#                return self.update_item
            self.add_item(item, mapping=mapping, config_data_dict=config_data)

            # alt:
            self.logger.debug("parse item: {}".format(item))
            conf_data = {}
            conf_data['id'] = self.get_iattr_value(item.conf, 'hue_apiv2_id')
            conf_data['resource'] = self.get_iattr_value(item.conf, 'hue_apiv2_resource')
            conf_data['function'] = self.get_iattr_value(item.conf, 'hue_apiv2_function')
            if self.has_iattr(item.conf, 'hue_apiv2_reference_light_id'):
                if conf_data['resource'] == "group":
                    conf_data['hue_apiv2_reference_light_id'] = self.get_iattr_value(item.conf, 'hue_apiv2_reference_light_id')

            conf_data['item'] = item
            # store config in plugin_items
            self.plugin_items[item.property.path] = conf_data
            # set flags to schedule updates for sensors, lights and groups
            if conf_data['resource'] == 'sensor':
                # ensure that the scheduler for sensors will be started if items use sensor data
                self.sensor_items_configured = True

            if conf_data['resource'] == 'group':
                # bridge updates are allways scheduled
                self.logger.debug("parse_item: configured group item = {}".format(conf_data))

            # updating=True, if not read only
            if not conf_data['function'] in ['reachable', 'battery'] and \
               not conf_data['function'] in self.hue_sensor_state_values:
                self.add_item(item, mapping=mapping, config_data_dict=config_data, updating=True)
                return self.update_item
            #self.add_item(item, mapping=mapping, config_data_dict=config_data)
            return

        if 'hue_apiv2_dpt3_dim' in item.conf:
            return self.dimDPT3


    def parse_logic(self, logic):
        """
        Default plugin parse_logic method
        """
        if 'xxx' in logic.conf:
            # self.function(logic['name'])
            pass


    def dimDPT3(self, item, caller=None, source=None, dest=None):
        # Evaluation of the list values for the KNX data
        # [1] for dimming
        # [0] for direction
        parent = item.return_parent()

        if item()[1] == 1:
            # dimmen
            if item()[0] == 1:
                # up
                parent(254, self.get_shortname()+"dpt3")
            else:
                # down
                parent(-254, self.get_shortname()+"dpt3")
        else:
            parent(0, self.get_shortname()+"dpt3")


    def update_item(self, item, caller=None, source=None, dest=None):
        """
        Item has been updated

        This method is called, if the value of an item has been updated by SmartHomeNG.
        It should write the changed value out to the device (hardware/interface) that
        is managed by this plugin.

        To prevent a loop, the changed value should only be written to the device, if the plugin is running and
        the value was changed outside of this plugin(-instance). That is checked by comparing the caller parameter
        with the fullname (plugin name & instance) of the plugin.

        :param item: item to be updated towards the plugin
        :param caller: if given it represents the callers name
        :param source: if given it represents the source
        :param dest: if given it represents the dest
        """
        if self.alive and caller != self.get_fullname():
            # code to execute if the plugin is not stopped
            # and only, if the item has not been changed by this plugin:
            self.logger.info(f"update_item: '{item.property.path}' has been changed outside this plugin by caller '{self.callerinfo(caller, source)}'")

            config_data = self.get_item_config(item)
            self.logger.notice(f"update_item: Sending '{item()}' of '{config_data['item']}' to bridge  ->  {config_data=}")

            if config_data['resource'] == 'light':
                self.update_light_from_item(config_data, item)
            elif config_data['resource'] == 'group':
                self.update_group_from_item(config_data, item)
            elif config_data['resource'] == 'scene':
                self.update_scene_from_item(config_data, item)
            elif config_data['resource'] == 'sensor':
                self.update_sensor_from_item(config_data, item)
            elif config_data['resource'] == 'button':
                pass
                # self.update_button_from_item(config_data, item)
            else:
                self.logger.error(f"Resource '{config_data['resource']}' is not implemented")

        return


    def update_light_from_item(self, config_data, item):
        value = item()
        self.logger.debug(f"update_light_from_item: config_data = {config_data}")
        hue_transition_time = self._default_transition_time
        if config_data['transition_time'] is not None:
            hue_transition_time = int(float(config_data['transition_time']) * 1000)

        #self.logger.notice(f"update_light_from_item: function={config_data['function']}, hue_transition_time={hue_transition_time}, id={config_data['id']}")
        if config_data['function'] == 'on':
            if value:
                self.run_asyncio_coro(self.v2bridge.lights.turn_on(config_data['id'], hue_transition_time))
            else:
                self.run_asyncio_coro(self.v2bridge.lights.turn_off(config_data['id'], hue_transition_time))
        elif config_data['function'] == 'bri':
            self.run_asyncio_coro(self.v2bridge.lights.set_brightness(config_data['id'], float(value), hue_transition_time))
        elif config_data['function'] == 'xy' and isinstance(value, list) and len(value) == 2:
            self.run_asyncio_coro(self.v2bridge.lights.set_color(config_data['id'], value[0], value[1], hue_transition_time))
        elif config_data['function'] == 'ct':
            self.run_asyncio_coro(self.v2bridge.lights.set_color_temperature(config_data['id'], value, hue_transition_time))
        elif config_data['function'] == 'dict':
            if value != {}:
                on = value.get('on', None)
                bri = value.get('bri', None)
                xy = value.get('xy', None)
                if xy is not None:
                    xy = (xy[0], xy[1])
                ct = value.get('ct', None)
                if bri or xy or ct:
                    on = True
                transition_time = value.get('transition_time', None)
                if transition_time is None:
                    transition_time = hue_transition_time
                else:
                    transition_time = int(float(transition_time)*1000)
                self.run_asyncio_coro(self.v2bridge.lights.set_state(config_data['id'], on, bri, xy, ct, transition_time=transition_time))
        elif config_data['function'] == 'bri_inc':
            self.logger.warning(f"Lights: {config_data['function']} not implemented")
        elif config_data['function'] == 'alert':
            self.logger.warning(f"Lights: {config_data['function']} not implemented")
        elif config_data['function'] == 'effect':
            self.logger.warning(f"Lights: {config_data['function']} not implemented")
        else:
            # The following functions from the api v1 are not supported by the api v2:
            # - hue, sat, ct
            # - name (for display, reading is done from the device-name)
            self.logger.notice(f"update_light_from_item: The function {config_data['function']} is not supported/implemented")
        return


    def update_scene_from_item(self, config_data, item):

        value = item()
        self.logger.debug(f"update_scene_from_item: config_data = {config_data}")
        hue_transition_time = self._default_transition_time
        if config_data['transition_time'] is not None:
            hue_transition_time = int(float(config_data['transition_time']) * 1000)

        if config_data['function'] == 'activate':
            self.run_asyncio_coro(self.v2bridge.scenes.recall(id=config_data['id']))
        elif config_data['function'] == 'activate_scene':
            #self.v2bridge.scenes.recall(id=value, dynamic=False, duration=hue_transition_time, brightness=float(bri))
            self.run_asyncio_coro(self.v2bridge.scenes.recall(id=value))
        elif config_data['function'] == 'name':
            self.logger.warning(f"Scenes: {config_data['function']} not implemented")
        return


    def update_group_from_item(self, config_data, item):
        value = item()
        self.logger.debug(f"update_group_from_item: config_data = {config_data} -> value = {value}")

        hue_transition_time = self._default_transition_time
        if config_data['transition_time'] is not None:
            hue_transition_time = int(float(config_data['transition_time']) * 1000)

        #self.logger.notice(f"update_group_from_item: function={config_data['function']}, hue_transition_time={hue_transition_time}, id={config_data['id']}")
        if config_data['function'] == 'on':
            self.run_asyncio_coro(self.v2bridge.groups.grouped_light.set_state(config_data['id'], on=value, transition_time=hue_transition_time))
        elif config_data['function'] == 'bri':
            self.run_asyncio_coro(self.v2bridge.groups.grouped_light.set_state(config_data['id'], on=True, brightness=float(value), transition_time=hue_transition_time))
        elif config_data['function'] == 'xy' and isinstance(value, list) and len(value) == 2:
            self.run_asyncio_coro(self.v2bridge.groups.grouped_light.set_state(config_data['id'], on=True, color_xy=value, transition_time=hue_transition_time))
        elif config_data['function'] == 'ct':
            self.run_asyncio_coro(self.v2bridge.groups.grouped_light.set_state(config_data['id'], on=True, color_temp=value, transition_time=hue_transition_time))
        elif config_data['function'] == 'dict':
            if value != {}:
                on = value.get('on', None)
                bri = value.get('bri', None)
                xy_in = value.get('xy', None)
                xy = None
                if xy_in is not None:
                    xy = (xy_in[0], xy_in[1])
                self.logger.notice(f"update_group_from_item: {xy_in=}, {xy=}, {type(xy)=}")
                ct = value.get('ct', None)
                if bri or xy or ct:
                    on = True
                transition_time = value.get('transition_time', None)
                if transition_time is None:
                    transition_time = hue_transition_time
                else:
                    transition_time = int(float(transition_time)*1000)
                self.run_asyncio_coro(self.v2bridge.groups.grouped_light.set_state(config_data['id'], on, bri, xy, ct, transition_time=transition_time))
        elif config_data['function'] == 'bri_inc':
            self.logger.warning(f"Groups: {config_data['function']} not implemented")
        elif config_data['function'] == 'alert':
            self.logger.warning(f"Groups: {config_data['function']} not implemented")
        elif config_data['function'] == 'effect':
            self.logger.warning(f"Groups: {config_data['function']} not implemented")
        else:
            # The following functions from the api v1 are not supported by the api v2:
            # - hue, sat, ct, name
            self.logger.notice(f"update_group_from_item: The function {config_data['function']} is not supported/implemented")
###

        return

        try:
            if plugin_item['function'] == 'activate_scene':
                self.br.groups(plugin_item['id'], 'action', scene=value, transitiontime=hue_transition_time)
            elif plugin_item['function'] == 'modify_scene':
                 self.br.groups(plugin_item['id'], 'scenes', value['scene_id'], 'lights', value['light_id'], 'state', **(value['state']))

        except qhue.qhue.QhueException as e:
            msg = f"{e}"
            msg = f"update_light_from_item: item {plugin_item['item'].id()} - function={plugin_item['function']} - '{msg}'"
            if msg.find(' 201 ') >= 0:
                self.logger.info(msg)
            else:
                self.logger.error(msg)

        return


    def update_sensor_from_item(self, config_data, value):

        self.logger.debug(f"update_sensor_from_item: config_data = {config_data}")
        if config_data['function'] == 'name':
            self.logger.warning(f"Sensors: {config_data['function']} not implemented")
        return


    def get_api_config_of_bridge(self, urlbase):

        url = urlbase + 'api/config'
        api_config = {}
        try:
            r = requests.get(url)
            if r.status_code == 200:
                api_config = r.json()
        except Exception as e:
            self.logger.error(f"get_api_config_of_bridge: url='{url}' - Exception {e}")
        return api_config


    def get_data_from_discovered_bridges(self, serialno):
        """
        Get data from discovered bridges for a given serial number

        :param serialno: serial number of the bridge to look for
        :return: bridge info
        """
        result = {}
        for db in self.discovered_bridges:
            if db['serialNumber'] == serialno:
                result = db
                break
        if result == {}:
            # if bridge is not in list of discovered bridges, rediscover bridges and try again
            self.discovered_bridges = self.discover_bridges()
            for db in self.discovered_bridges:
                if db['serialNumber'] == serialno:
                    result = db
                    break

        if result != {}:
            api_config = self.get_api_config_of_bridge(result.get('URLBase',''))
            result['datastoreversion'] = api_config.get('datastoreversion', '')
            result['apiversion'] = api_config.get('apiversion', '')
            result['swversion'] = api_config.get('swversion', '')
            result['modelid'] = api_config.get('modelid', '')

        return result


    def poll_bridge(self):
        """
        Polls for updates of the device

        This method is only needed, if the device (hardware/interface) does not propagate
        changes on it's own, but has to be polled to get the actual status.
        It is called by the scheduler which is set within run() method.
        """
        # # get the value from the device
        # device_value = ...
        #self.get_lights_info()
        if self.bridge.get('serialNumber','') == '':
            self.bridge_config = {}
            self.bridge_scenes = {}
            self.bridge_sensors = {}
            return
        else:
            if self.br is not None:
                try:
                    if not self.sensor_items_configured:
                        self.bridge_sensors = self.br.sensors()
                except Exception as e:
                    self.logger.error(f"poll_bridge: Exception {e}")

                try:
                    self.bridge_config = self.br.config()
                except Exception as e:
                    self.logger.info(f"poll_bridge: Bridge-config not supported - Exception {e}")

                try:
                    self.bridge_scenes = self.br.scenes()
                except Exception as e:
                    self.logger.info(f"poll_bridge: Scenes not supported - Exception {e}")

        # update items with polled data
        src = self.get_instance_name()
        if src == '':
            src = None
        for pi in self.plugin_items:
            plugin_item = self.plugin_items[pi]
            if plugin_item['resource'] == 'scene':
                value = self._get_scene_item_value(plugin_item['id'], plugin_item['function'], plugin_item['item'].id())
                if value is not None:
                    plugin_item['item'](value, self.get_shortname(), src)
            if plugin_item['resource'] == 'group':
                if not "hue_apiv2_reference_light_id" in plugin_item:
                    if plugin_item['function'] != 'dict' and plugin_item['function'] != 'modify_scene':
                        if plugin_item['function'] == 'on':
                            value = self._get_group_item_value(plugin_item['id'], 'any_on', plugin_item['item'].id())
                        else:
                            value = self._get_group_item_value(plugin_item['id'], plugin_item['function'], plugin_item['item'].id())
                        if value is not None:
                            plugin_item['item'](value, self.get_shortname(), src)
        return


    def poll_bridge_sensors(self):
        """
        Polls for updates of sensors of the device

        This method is only needed, if the device (hardware/interface) does not propagate
        changes on it's own, but has to be polled to get the actual status.
        It is called by the scheduler which is set within run() method.
        """
        # get the value from the device: poll data from bridge
        if self.bridge.get('serialNumber','') == '':
            self.bridge_sensors = {}
            return
        else:
            if self.br is not None:
                try:
                    self.bridge_sensors = self.br.sensors()
                except Exception as e:
                    self.logger.error(f"poll_bridge_sensors: Exception {e}")

        # update items with polled data
        src = self.get_instance_name()
        if src == '':
            src = None
        for pi in self.plugin_items:
            plugin_item = self.plugin_items[pi]
            if  plugin_item['resource'] == 'sensor':
                value = self._get_sensor_item_value(plugin_item['id'], plugin_item['function'], plugin_item['item'].id())
                if value is not None:
                    plugin_item['item'](value, self.get_shortname(), src)
        return


    def _get_group_item_value(self, group_id, function, item_path):
        """
        Update item that has hue_resource == 'group'
        :param id:
        :param function:
        :return:
        """
        result = ''

        return result


    def _get_scene_item_value(self, scene_id, function, item_path):
        """
        Update item that has hue_resource == 'scene'
        :param id:
        :param function:
        :return:
        """
        result = ''
        try:
            scene = self.bridge_scenes[scene_id]
        except KeyError:
            self.logger.error(f"poll_bridge: Scene '{scene_id}' not defined on bridge (item '{item_path}')")
            return None

        if function == 'name':
            result = scene['name']
        return result


    def _get_sensor_item_value(self, sensor_id, function, item_path):
        """
        Update item that has hue_resource == 'sensor'
        :param id:
        :param function:
        :return:
        """
        result = ''
        try:
            sensor = self.bridge_sensors[sensor_id]
        except KeyError:
            self.logger.error(f"poll_bridge_sensors: Sensor '{sensor_id}' not defined on bridge (item '{item_path}')")
            return None
        except Exception as e :
            self.logger.exception(f"poll_bridge_sensors: Sensor '{sensor_id}' on bridge (item '{item_path}') - exception: {e}")
            return None
        if function in self.hue_sensor_state_values:
            try:
                result = sensor['state'][function]
            except KeyError:
                self.logger.warning(
                    f"poll_bridge_sensors: Function {function} not supported by sensor '{sensor_id}' (item '{item_path}')")
                result = ''
        elif function in self.hue_sensor_config_values:
            try:
                result = sensor['config'][function]
            except KeyError:
                self.logger.warning(
                    f"poll_bridge_sensors: Function {function} not supported by sensor '{sensor_id}' (item '{item_path}')")
                result = ''
        elif function == 'name':
            result = sensor['name']
        return result


    def update_plugin_config(self):
        """
        Update the plugin configuration of this plugin in ../etc/plugin.yaml

        Fill a dict with all the parameters that should be changed in the config file
        and call the Method update_config_section()
        """
        conf_dict = {}
        # conf_dict['bridge'] = self.bridge
        conf_dict['bridge_serial'] = self.bridge.get('serialNumber','')
        conf_dict['bridge_user'] = self.bridge.get('username','')
        conf_dict['bridge_ip'] = self.bridge.get('ip','')
        conf_dict['bridge_port'] = self.bridge.get('port','')
        self.update_config_section(conf_dict)
        return

    # ============================================================================================

    def get_bridgeinfo(self):
        if self.bridge.get('serialNumber','') == '':
            self.br = None
            self.bridge_config = {}
            self.bridge_scenes = {}
            self.bridge_sensors = {}
            return
        self.logger.info("get_bridgeinfo: self.bridge = {}".format(self.bridge))
        self.br = qhue.Bridge(self.bridge['ip']+':'+str(self.bridge['port']), self.bridge['username'])
        try:
            self.bridge_config = self.br.config()
            self.bridge_scenes = self.br.scenes()
            self.bridge_sensors = self.br.sensors()
        except Exception as e:
            self.logger.error(f"Bridge '{self.bridge.get('serialNumber','')}' returned exception {e}")
            self.br = None
            self.bridge_config = {}
            self.bridge_scenes = {}
            self.bridge_sensors = {}
            return False

        return True


    def get_bridge_desciption(self, ip, port):
        """
        Get description of bridge

        :param ip:
        :param port:
        :return:
        """
        br_info = {}

        protocol = 'http'
        if str(port) == '443':
            protocol = 'https'

        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(protocol + '://' + ip + ':' + str(port) + '/description.xml', verify=False)
        if r.status_code == 200:
            xmldict = xmltodict.parse(r.text)
            br_info['ip'] = ip
            br_info['port'] = str(port)
            br_info['friendlyName'] = str(xmldict['root']['device']['friendlyName'])
            br_info['manufacturer'] = str(xmldict['root']['device']['manufacturer'])
            br_info['manufacturerURL'] = str(xmldict['root']['device']['manufacturerURL'])
            br_info['modelDescription'] = str(xmldict['root']['device']['modelDescription'])
            br_info['modelName'] = str(xmldict['root']['device']['modelName'])
            br_info['modelURL'] = str(xmldict['root']['device']['modelURL'])
            br_info['modelNumber'] = str(xmldict['root']['device']['modelNumber'])
            br_info['serialNumber'] = str(xmldict['root']['device']['serialNumber'])
            br_info['UDN'] = str(xmldict['root']['device']['UDN'])
            br_info['gatewayName'] = str(xmldict['root']['device'].get('gatewayName', ''))

            br_info['URLBase'] = str(xmldict['root']['URLBase'])
            if br_info['modelName'] == 'Philips hue bridge 2012':
                br_info['version'] = 'v1'
            elif br_info['modelName'] == 'Philips hue bridge 2015':
                br_info['version'] = 'v2'
            else:
                br_info['version'] = 'unknown'

            # get API information
            api_config = self.get_api_config_of_bridge(br_info['URLBase'])
            br_info['datastoreversion'] = api_config.get('datastoreversion', '')
            br_info['apiversion'] = api_config.get('apiversion', '')
            br_info['swversion'] = api_config.get('swversion', '')
            br_info['modelid'] = api_config.get('modelid', '')

        return br_info


    def discover_bridges(self):
        bridges = []
        try:
            #discovered_bridges = discover_bridges(mdns=True, upnp=True, httponly=True)
            discovered_bridges = discover_bridges(upnp=True, httponly=True)

        except Exception as e:
            self.logger.error("discover_bridges: Exception in discover_bridges(): {}".format(e))
            discovered_bridges = {}

        for br in discovered_bridges:
            ip = discovered_bridges[br].split('/')[2].split(':')[0]
            port = discovered_bridges[br].split('/')[2].split(':')[1]
            br_info = self.get_bridge_desciption(ip, port)

            bridges.append(br_info)

        for bridge in bridges:
            self.logger.info("Discoverd bridge = {}".format(bridge))

        return bridges

    # --------------------------------------------------------------------------------------------

    def create_new_username(self, ip, port, devicetype=None, timeout=5):
        """
        Helper function to generate a new anonymous username on a hue bridge

        This method is a copy from the queue package without keyboard input

        :param ip:          ip address of the bridge
        :param devicetype:  (optional) devicetype to register with the bridge. If unprovided, generates a device
                            type based on the local hostname.
        :param timeout:     (optional, default=5) request timeout in seconds

        :return:            username/application key

        Raises:
            QhueException if something went wrong with username generation (for
                example, if the bridge button wasn't pressed).
        """
        api_url = "http://{}/api".format(ip+':'+port)
        try:
            # for qhue versions v2.0.0 and up
            session = requests.Session()
            res = qhue.qhue.Resource(api_url, session, timeout)
        except:
            # for qhue versions prior to v2.0.0
            res = qhue.qhue.Resource(api_url, timeout)
            res = qhue.qhue.Resource(api_url, timeout)

        if devicetype is None:
            devicetype = "SmartHomeNG#{}".format(getfqdn())

        # raises QhueException if something went wrong
        try:
            response = res(devicetype=devicetype, http_method="post")
        except Exception as e:
            self.logger.warning("create_new_username: Exception {}".format(e))
            return ''
        else:
            self.logger.info("create_new_username: Generated username = {}".format(response[0]["success"]["username"]))
            return response[0]["success"]["username"]


    def remove_username(self, ip, port, username, timeout=5):
        """
        Remove the username/application key from the bridge

        This function works only up to api version 1.3.0 of the bridge. Afterwards Philips/Signify disbled
        the removal of users through the api. It is now only possible through the portal (cloud serivce).

        :param ip:          ip address of the bridge
        :param username:
        :param timeout:     (optional, default=5) request timeout in seconds
        :return:

        Raises:
            QhueException if something went wrong with username deletion
        """
        api_url = "http://{}/api/{}".format(ip+':'+port, username)
        url = api_url + "/config/whitelist/{}".format(username)
        self.logger.info("remove_username: url = {}".format(url))
        res = qhue.qhue.Resource(url, timeout)

        devicetype = "SmartHomeNG#{}".format(getfqdn())

        # raises QhueException if something went wrong
        try:
            response = res(devicetype=devicetype, http_method="delete")
        except Exception as e:
            self.logger.error("remove_username: res-delete exception {}".format(e))
            response = [{'error': str(e)}]

        if not('success' in response[0]):
            self.logger.warning("remove_username: Error removing username/application key {} - {}".format(username, response[0]))
        else:
            self.logger.info("remove_username: username/application key {} removed".format(username))


