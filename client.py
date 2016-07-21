#!/usr/bin/env python
"""
Copyright (c) 2016 superfsm@gmail.com

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.

"""

import os
import re
import json
import struct
import logging
import requests
import argparse
import getpass
import pprint
import time

from collections import defaultdict

from pgoapi import PGoApi
from pgoapi.utilities import f2i, h2f

from google.protobuf.internal import encoder
from geopy.geocoders import GoogleV3
from geopy.distance import great_circle
from s2sphere import CellId, LatLng

log = logging.getLogger(__name__)

class MyDict(dict):

    def __missing__(self, key):
        return MyDict({})

    def __getitem__(self, key):
        val = dict.__getitem__(self, key)
        if isinstance(val, dict) and not isinstance(val, MyDict):
            val = MyDict(val)
        return val

# def chain_api(func):
#     def wrapper(self, *args, **kwargs):
#         func(self, *args, **kwargs)
#         return self
#     return wrapper

class Client:

    def __init__(self):
        self._api = PGoApi()

        self._lat = 0
        self._lng = 0
        self._alt = 0

        self._lat_f2i = 0
        self._lng_f2i = 0
        self._alt_f2i = 0

        self.profile = {}
        self.item = defaultdict(int)

        self.pokestop = {}

        self.pokemon = []
        self.wild_pokemon = []

    def get_position(self):
        return (self._lat, self._lng)

    # Move to object

    def move_to_obj(self, obj, speed = 20):
        self.move_to(obj['latitude'], obj['longitude'], speed = speed)

    # Move to position at speed(m)/s

    def move_to(self, lat, lng, speed = 20):
        a = (self._lat, self._lng)
        b = (lat, lng)

        dist = great_circle(a, b).meters
        steps = int(dist / speed) + 1

        delta_lat = (lat - self._lat) / steps
        delta_lng = (lng - self._lng) / steps

        log.info('Moving ... %d steps' % steps)
        for step in range(steps):
            self.jump_to(self._lat + delta_lat, self._lng + delta_lng)
            time.sleep(1)

    # Jump to position

    def jump_to(self, lat, lng, alt = 0):
        log.debug('Move to - Lat: %s Long: %s Alt: %s', lat, lng, alt)

        self._api.set_position(lat, lng, 0)

        self._lat = lat
        self._lng = lng
        # self._alt = alt

        self._lat_f2i = f2i(lat)
        self._lng_f2i = f2i(lng)
        # self._alt_f2i = f2i(alt)

    # Distance to an object
    def _dist_to_obj(self, obj):
        a = (self._lat, self._lng)
        b = (obj['lat'], obj['lng'])
        return great_circle(a, b).meters

    # Sort the items on map by distance
    def sort_map(self):
        for i in self.pokestop:
            i['dist'] = self._dist_to_obj(i)
        self.pokestop = sorted(self.pokestop, key=lambda k: k['dist'])
        for i in self.pokewild:
            i['dist'] = self._dist_to_obj(i)
        self.pokewild = sorted(self.pokewild, key=lambda k: k['dist'])

    # Send request and parse response
    def _call(self):

        # Call api
        resp = self._api.call()
        log.debug('Response dictionary: \n\r{}'.format(pprint.PrettyPrinter(indent=2, width=60).pformat(resp)))

        if not resp:
            return

        responses = MyDict(resp)['responses']

        # GET_MAP_OBJECTS
        for map_cell in responses['GET_MAP_OBJECTS']['map_cells']:
            map_cell = MyDict(map_cell)

            for fort in map_cell['forts']:
                fort = MyDict(fort)
                if fort['type'] == 1:
                    self.pokestop[fort['id']] = fort
                    # log.debug('POKESTOP = {}'.format(fort))

            for wild_pokemon in map_cell['wild_pokemons']:
                self.wild_pokemon.append(wild_pokemon)
                # log.debug('POKEMON = {}'.format(wild_pokemon))

        # FORT_SEARCH
        if responses['FORT_SEARCH']:
            fort_exp = responses['FORT_SEARCH']['experience_awarded']
            fort_result = responses['FORT_SEARCH']['result']
            if fort_result == 1:
                log.info('FORT_SEARCH exp = {}'.format(fort_exp))
            else:
                log.warning('FORT_SEARCH result = {}'.format(fort_result))

        # GET_INVENTORY
        if responses['GET_INVENTORY']['success']:
            for inventory_item in responses['GET_INVENTORY']['inventory_delta']['inventory_items']:
                inventory_item = MyDict(inventory_item)

                if inventory_item['deleted_item_key']:
                    log.warning('*** captured deleted_item_key in inventory')
                    log.warning(inventory_item)

                #Item
                item = inventory_item['inventory_item_data']['item']
                count =inventory_item['inventory_item_data']['count']
                if item and count:
                    self.item[item] = count
                    # log.debug('ITEM {} = {}'.format(item, count))

                #Stats
                player_stats = inventory_item['inventory_item_data']['player_stats']
                self.profile.update(player_stats)
                # log.debug('PROFILE {}'.format(self.profile))

                #Pokemon
                pokemon = inventory_item['inventory_item_data']['pokemon_data']['cp']
                if pokemon:
                    self.pokemon.append(pokemon)

        # GET_PLAYER
        if 'GET_PLAYER' in responses:
            if 'profile' in responses['GET_PLAYER']:
                self.profile['username'] = responses['GET_PLAYER']['profile']['username']
                # log.debug('PROFILE username = {}'.format(self.profile['username']))

        # ENCOUNTER
        if 'ENCOUNTER' in responses:
            if responses['ENCOUNTER']['status'] == 1:
                log.info('ENCOUNTER success')
                return True
            else:
                log.warning('ENCOUNTER failed')
                return False

        # CATCH_POKEMON
        if 'CATCH_POKEMON' in responses:
            if responses['CATCH_POKEMON']['status'] == 1:
                log.info('CATCH exp = {}'.format(sum(responses['CATCH_POKEMON']['capture_award']['xp'])))
                return True
            else:
                log.warning('CATCH failed')
                return False

    def summary(self):

        print 'POSITION (lat,lng) = {},{}'.format(self._lat, self._lng)
        print 'POKEMON =', len(self.pokemon)
        print 'POKESTOP =', len(self.pokestop)
        print 'WILD POKEMON =', len(self.wild_pokemon)
        print 'ITEM =', pprint.PrettyPrinter(indent=2).pformat(self.item)
        print 'PROFILE =', pprint.PrettyPrinter(indent=2).pformat(self.profile)

    # Scan the map around you
    def scan(self):

        self.wild_pokemon = []

        cell_ids = self._get_cell_ids()
        timestamps = [0, ] * len(cell_ids)
        self._get_inventory()
        self._get_player()
        self._api.get_map_objects(
            latitude=self._lat_f2i,
            longitude=self._lng_f2i,
            since_timestamp_ms=timestamps,
            cell_id=cell_ids)
        self._call()


    def _encounter(self, pokemon):
        self._api.encounter(
            encounter_id=pokemon['encounter_id'],
            spawnpoint_id=pokemon['spawnpoint_id'],
            player_latitude=self._lat_f2i,
            player_longitude=self._lng_f2i)


    def catch_pokemon(self, pokemon):
        self._encounter(pokemon)
        ret = self._call()
        if ret:
            self._api.catch_pokemon(
                encounter_id=pokemon['encounter_id'],
                pokeball=1,
                normalized_reticle_size=1.950,
                spawn_point_guid=pokemon['spawnpoint_id'],
                hit_pokemon=1,
                spin_modifier=1,
                normalized_hit_position=1)
            self._call()


    # Spin the pokestop
    def fort_search(self, pokestop):
        self._api.fort_search(
            fort_id=pokestop['id'],
            fort_latitude=pokestop['latitude'],
            fort_longitude=pokestop['longitude'],
            player_latitude=self._lat_f2i,
            player_longitude=self._lng_f2i)
        self._call()

    # Login
    def login(self, auth_service, username, password):
        return self._api.login(auth_service, username, password)

    def test(self):
        self._api.get_player()
        self._api.get_inventory()
        resp = self._api._call()
        log.info('Response dictionary: \n\r{}'.format(json.dumps(resp, indent=2)))


    def _get_player(self):
        self._api.get_player()


    def _get_inventory(self):
        self.pokemon = []
        self._api.get_inventory()

    def _get_cell_ids(self, radius=10):
        lat = self._lat
        long = self._lng
        origin = CellId.from_lat_lng(LatLng.from_degrees(lat, long)).parent(15)
        walk = [origin.id()]
        right = origin.next()
        left = origin.prev()

        # Search around provided radius
        for i in range(radius):
            walk.append(right.id())
            walk.append(left.id())
            right = right.next()
            left = left.prev()

        # Return everything
        return sorted(walk)

    def _encode(self,cellid):
        output = []
        encoder._VarintEncoder()(output.append, cellid)
        return ''.join(output)

def main():
    pass

if __name__ == '__main__':
    main()
