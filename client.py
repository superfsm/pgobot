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


import json
import logging
import pprint
import time
import math
import csv
from collections import defaultdict

from pgoapi import PGoApi

from pgoapi.protos.POGOProtos.Inventory_pb2 import ItemId
from pgoapi.protos.POGOProtos.Enums_pb2 import PokemonId
from pgoapi.protos.POGOProtos.Networking.Responses_pb2 import (
    FortSearchResponse, EncounterResponse, CatchPokemonResponse, ReleasePokemonResponse,
    RecycleInventoryItemResponse, UseItemEggIncubatorResponse)

from google.protobuf.internal import encoder
from geopy.distance import great_circle
from s2sphere import CellId, LatLng

log = logging.getLogger(__name__)

POKEMON_ID_MAX = 151


class MyDict(dict):

    def __missing__(self, key):
        return MyDict({})

    def __getitem__(self, key):
        val = dict.__getitem__(self, key)
        if isinstance(val, dict) and not isinstance(val, MyDict):
            val = MyDict(val)
        return val

with open("GAME_MASTER_POKEMON.tsv") as tsv:
    lines = [line for line in csv.reader(tsv, delimiter="\t")]
    POKEDEX = {}
    for idx in range(1,POKEMON_ID_MAX+1):
        POKE = {}
        for idx_k in range(len(lines[0])):
            try:
                POKE[lines[0][idx_k]] = int(lines[idx][idx_k])
            except ValueError:
                POKE[lines[0][idx_k]] = lines[idx][idx_k]
        POKEDEX[idx] = POKE
        POKEDEX[POKE['Identifier']] = POKE
    POKEDEX[133]['EvolvesTo'] = 'Vaporeon'

def chain_api(func):
    def wrapper(self, *args, **kwargs):
        func(self, *args, **kwargs)
        return self
    return wrapper


class Client:

    def __init__(self):
        self._api = PGoApi()

        self._lat = 0
        self._lng = 0
        self._alt = 0

        self.profile = {}
        self.incubator = {}
        self.item = defaultdict(int)
        self.pokemon = defaultdict(list)
        self.candy = defaultdict(int)
        self.egg = []

        self.pokestop = {}
        self.wild_pokemon = []

    def get_pokestop(self):
        return self.pokestop.values()

    def get_wild_pokemon(self):
        return self.wild_pokemon[:]

    def get_position(self):
        return (self._lat, self._lng)

    # Move to object
    @chain_api
    def move_to_obj(self, obj, speed=20):
        self.move_to(obj['latitude'], obj['longitude'], speed=speed)

    # Move to position at speed(m)/s
    @chain_api
    def move_to(self, lat, lng, speed=20):
        a = (self._lat, self._lng)
        b = (lat, lng)

        dist = great_circle(a, b).meters
        steps = int(dist / speed) + 1

        delta_lat = (lat - self._lat) / steps
        delta_lng = (lng - self._lng) / steps

        log.info('Moving ... %d steps' % steps)
        prev_time = time.time()
        for step in range(steps):
            self.jump_to(self._lat + delta_lat, self._lng + delta_lng)
            time.sleep(1)
            if time.time() - prev_time > 30:
                self.scan()
                prev_time = time.time()

    # Jump to position
    @chain_api
    def jump_to(self, lat, lng, alt=0):
        log.debug('Move to - Lat: %s Long: %s Alt: %s', lat, lng, alt)

        self._api.set_position(lat, lng, 0)

        self._lat = lat
        self._lng = lng
        # self._alt = alt

    # Distance to an object
    def _dist_to_obj(self, obj):
        a = (self._lat, self._lng)
        b = (obj['lat'], obj['lng'])
        return great_circle(a, b).meters

    # Send request and parse response
    def _call(self):

        # Call api
        resp = self._api.call()
        log.debug('Response dictionary: \n\r{}'.format(
            pprint.PrettyPrinter(indent=2, width=3).pformat(resp)))

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

        # GET_HATCHED_EGGS
        # if 'GET_HATCHED_EGGS' in responses:
        #     if responses['GET_HATCHED_EGGS']['success'] is True:
        #         if responses['GET_HATCHED_EGGS']['exp']:
        #             log.info('GET_HATCHED_EGGS exp = {}'.format(
        #                 responses['GET_HATCHED_EGGS']['experience_awarded']))
        #     else:
        #         log.warning('GET_HATCHED_EGGS {}'.format(responses['GET_HATCHED_EGGS']['success']))

        # FORT_SEARCH
        if responses['FORT_SEARCH']:
            experience_awarded = responses['FORT_SEARCH']['experience_awarded']
            result = responses['FORT_SEARCH']['result']
            if result:
                log.info('FORT_SEARCH {}, EXP = {}'.format(
                    FortSearchResponse.Result.Name(result), experience_awarded))
                if result == FortSearchResponse.Result.Value('INVENTORY_FULL'):
                    self.summary()
                    self.bulk_recycle_inventory_item()
            else:
                log.warning('FORT_SEARCH result = {}'.format(result))

        # GET_INVENTORY
        if responses['GET_INVENTORY']['success']:
            for inventory_item in responses['GET_INVENTORY']['inventory_delta']['inventory_items']:
                inventory_item = MyDict(inventory_item)

                if inventory_item['deleted_item_key']:
                    log.warning('*** captured deleted_item_key in inventory')
                    log.warning(inventory_item)

                # Item
                item_id = inventory_item['inventory_item_data']['item']['item_id']
                count = inventory_item['inventory_item_data']['item']['count']
                if item_id and count:
                    self.item[item_id] = count
                    # log.debug('ITEM {} = {}'.format(item, count))

                # Stats
                player_stats = inventory_item['inventory_item_data']['player_stats']
                self.profile.update(player_stats)
                # log.debug('PROFILE {}'.format(self.profile))

                # Pokemon
                pokemon = inventory_item['inventory_item_data']['pokemon_data']
                if pokemon['cp']:
                    pokemon['max_cp'] = self._max_cp(pokemon)
                    self.pokemon[pokemon['pokemon_id']].append(pokemon)

                elif pokemon['is_egg'] is True:
                    self.egg.append(pokemon)

                # sort by max_cp
                for idx in range(1, POKEMON_ID_MAX + 1):
                    self.pokemon[idx].sort(reverse=True, key=lambda p: p['max_cp'])

                # Candy
                pokemon_family = inventory_item['inventory_item_data']['pokemon_family']
                candy = pokemon_family['candy']
                family_id = pokemon_family['family_id']
                if candy and family_id:
                    self.candy[family_id] = candy

                # Incubators
                egg_incubators = inventory_item['inventory_item_data']['egg_incubators']['egg_incubator']
                if egg_incubators:
                    for egg_incubator in egg_incubators:
                        self.incubator[egg_incubator['id']] = egg_incubator

        # GET_PLAYER
        if responses['GET_PLAYER']['success'] is True:
            self.profile.update(responses['GET_PLAYER']['player_data'])

        # ENCOUNTER
        if 'ENCOUNTER' in responses:
            if responses['ENCOUNTER']['status']:
                log.info('ENCOUNTER = {}'.format(
                    EncounterResponse.Status.Name(responses['ENCOUNTER']['status'])))
            else:
                log.warning('ENCOUNTER = {}')

            if responses['ENCOUNTER']['status'] == 1:
                max_cp = self._max_cp(responses['ENCOUNTER']['wild_pokemon']['pokemon_data'])
                log.info('ENCOUNTER MAX_CP = {} PROB = {}'.format(
                    max_cp, responses['ENCOUNTER']['capture_probability']['capture_probability']))
                # Bool, CP, ID
                return (
                    True,
                    max_cp,
                    responses['ENCOUNTER']['wild_pokemon']['pokemon_data']['pokemon_id'])
            else:
                if responses['ENCOUNTER']['status'] == EncounterResponse.Status.Value('POKEMON_INVENTORY_FULL'):
                    self.bulk_release_pokemon()
                return (False, )

        # CATCH_POKEMON
        if 'CATCH_POKEMON' in responses:

            status = responses['CATCH_POKEMON']['status']

            if status:
                log.info('CATCH_POKEMON = {}'.format(CatchPokemonResponse.CatchStatus.Name(status)))
            else:
                log.warning('CATCH_POKEMON = {}')

            if status == 1:
                log.info('CATCH_POKEMON EXP = {}'.format(
                    sum(responses['CATCH_POKEMON']['capture_award']['xp'])))
            return status

        # RELEASE_POKEMON
        if 'RELEASE_POKEMON' in responses:
            candy_awarded = responses['RELEASE_POKEMON']['candy_awarded']
            result = responses['RELEASE_POKEMON']['result']
            if result:
                log.info('RELEASE_POKEMON = {}, +{}'.format(
                    ReleasePokemonResponse.Result.Name(result), candy_awarded))
            else:
                log.warning('RELEASE_POKEMON = {}')

        # RECYCLE_INVENTORY_ITEM
        if 'RECYCLE_INVENTORY_ITEM' in responses:
            new_count = responses['RECYCLE_INVENTORY_ITEM']['new_count']
            result = responses['RECYCLE_INVENTORY_ITEM']['result']
            if result:
                log.info('RECYCLE_INVENTORY_ITEM = {}, {} left'.format(
                    RecycleInventoryItemResponse.Result.Name(result), new_count))
            else:
                log.warning('RECYCLE_INVENTORY_ITEM = {}')

        # USE_ITEM_CAPTURE
        if 'USE_ITEM_CAPTURE' in responses:
            log.info('USE_ITEM_CAPTURE success = {}'.format(responses['USE_ITEM_CAPTURE']['success']))

            if responses['USE_ITEM_CAPTURE']['success'] is True:
                return True
            else:
                return False

        # USE_ITEM_EGG_INCUBATOR
        if 'USE_ITEM_EGG_INCUBATOR' in responses:
            log.info('USE_ITEM_EGG_INCUBATOR result = {}'.format(
                UseItemEggIncubatorResponse.Result.Name(responses['USE_ITEM_EGG_INCUBATOR']['result'])))

    @chain_api
    def bulk_recycle_inventory_item(self):

        cnt_poke_ball = 0
        cnt_great_ball = 0
        cnt_ultra_ball = 0

        for item_id, count in self.item.iteritems():

            if item_id == ItemId.Value('ITEM_POTION'):
                self.recycle_inventory_item(item_id, count)
            if item_id == ItemId.Value('ITEM_SUPER_POTION'):
                self.recycle_inventory_item(item_id, count)
            if item_id == ItemId.Value('ITEM_HYPER_POTION') and count > 50:
                self.recycle_inventory_item(item_id, count - 50)
            if item_id == ItemId.Value('ITEM_REVIVE') and count > 30:
                self.recycle_inventory_item(item_id, count - 30)
            if item_id == ItemId.Value('ITEM_RAZZ_BERRY') and count > 30:
                self.recycle_inventory_item(item_id, count - 30)
            if item_id == ItemId.Value('ITEM_POKE_BALL'):
                cnt_poke_ball = count
            if item_id == ItemId.Value('ITEM_GREAT_BALL'):
                cnt_great_ball = count
            if item_id == ItemId.Value('ITEM_ULTRA_BALL'):
                cnt_ultra_ball = count

        if cnt_great_ball + cnt_ultra_ball > 100:
            self.recycle_inventory_item(ItemId.Value('ITEM_POKE_BALL'), cnt_poke_ball)
            self.recycle_inventory_item(ItemId.Value('ITEM_GREAT_BALL'), cnt_great_ball-100)
        elif cnt_great_ball + cnt_ultra_ball + cnt_poke_ball> 100:
            self.recycle_inventory_item(
                ItemId.Value('ITEM_POKE_BALL'),
                cnt_great_ball + cnt_ultra_ball + cnt_poke_ball - 100)

    @chain_api
    def recycle_inventory_item(self, item_id, count):
        self._api.recycle_inventory_item(item_id=item_id,count=count)
        self._call()

    def _max_cp(self,pokemon):
        pokemon_id = pokemon['pokemon_id']
        while POKEDEX[pokemon_id]['EvolvesTo']:
            pokemon_id = POKEDEX[POKEDEX[pokemon_id]['EvolvesTo']]['PkMn']

        attack = POKEDEX[pokemon_id]['BaseAttack']
        defense = POKEDEX[pokemon_id]['BaseDefense']
        stamina = POKEDEX[pokemon_id]['BaseStamina']
        if pokemon['individual_attack']:
            attack += pokemon['individual_attack']
        if pokemon['individual_defense']:
            defense += pokemon['individual_defense']
        if pokemon['individual_stamina']:
            stamina += pokemon['individual_stamina']
        max_cp = (attack * (defense**0.5) * (stamina**0.5) * (0.79030001**2)) / 10
        return max_cp

    @chain_api
    def bulk_release_pokemon(self):

        ranking = []
        for idx in range(1, POKEMON_ID_MAX + 1):
            ranking += self.pokemon[idx]

        ranking = sorted(ranking, key=lambda p: p['max_cp'])
        ranking = [(p['pokemon_id'],p['id'],p['max_cp']) for p in ranking]
        print [(PokemonId.Name(p[0]), p[2]) for p in ranking]
        #ranking max_cp low->high

        removed = 0
        idx = 0
        while removed <= 50:
            pokemon_id = ranking[idx][0]
            _id = ranking[idx][1]
            max_cp = ranking[idx][2]
            idx += 1
            if len(self.pokemon[pokemon_id]) <= 1 or self.pokemon[pokemon_id][0]['id'] == _id:
                continue
            else:
                print idx,'RELEASE_POKEMON max_cp =',max_cp
                removed += 1
                self.release_pokemon(_id)

    @chain_api
    def release_pokemon(self, pokemon_id):
        self._api.release_pokemon(pokemon_id=pokemon_id)
        self._call()

    @chain_api
    def status(self):
        cnt_item = 0
        for v in self.item.values():
            cnt_item += v

        cnt_pokemon = 0
        for idx in range(1, POKEMON_ID_MAX + 1):
            cnt_pokemon += len(self.pokemon[idx])

        exp = self.profile['experience'] - self.profile['prev_level_xp']
        exp_total = self.profile['next_level_xp'] - self.profile['prev_level_xp']

        print '[Lv %d, %d/%d, (%.2f%%)]\nWALK = %.3f ITEM = %d/%d, POKEMON = %d/%d' % (
            self.profile['level'], exp, exp_total, float(exp) / exp_total * 100,
            self.profile['km_walked'], cnt_item, self.profile['max_item_storage'],
            cnt_pokemon, self.profile['max_pokemon_storage'])

    @chain_api
    def summary(self):
        print 'PROFILE ='
        pprint.pprint(self.profile, indent=4)

        cnt_pokemon = 0
        for idx in range(1, POKEMON_ID_MAX + 1):

            if not POKEDEX[idx]['EvolvesTo']:
                candy = '-'
            else:
                candy = ''

            print '%03d (%15s)[%1s]: %3d =' % (
                idx, PokemonId.Name(idx), candy, self.candy[idx]), [(p['cp'],int(round(p['max_cp']))) for p in self.pokemon[idx]]
            cnt_pokemon += len(self.pokemon[idx])

        cnt_item = 0
        for k,v in self.item.iteritems():
            print "*%3d (%s) = %d" % (k, ItemId.Name(k), v)
            cnt_item += v

        for _,v in self.incubator.iteritems():
            if 'pokemon_id' in v:
                current_km = round(self.profile['km_walked'] - v['start_km_walked'],3)
                target_km = round(v['target_km_walked'] - v['start_km_walked'],3)
                print 'INCUBATOR: {}/{}'.format(current_km, target_km)

        print "ITEM # =\n   ", cnt_item
        print 'POKEMON # =\n   ', cnt_pokemon
        print 'POSITION (lat,lng) = {},{}'.format(self._lat, self._lng)
        print 'POKESTOP # =', len(self.pokestop)
        print 'WILD POKEMON # =', len(self.wild_pokemon)
        print 'EGG # =\n   ', len(self.egg)

        exp = self.profile['experience'] - self.profile['prev_level_xp']
        exp_total = self.profile['next_level_xp'] - self.profile['prev_level_xp']
        print '====Lv %d, %d/%d (%.2f%%)' % (
            self.profile['level'], exp, exp_total, float(exp)/exp_total*100)

    # Scan the map around you
    @chain_api
    def scan(self):

        self.wild_pokemon = []

        cell_ids = self._get_cell_ids()
        timestamps = [0, ] * len(cell_ids)
        self._get_inventory()
        self._get_player()
        self._get_hatched_eggs()
        self._api.get_map_objects(
            latitude=self._lat,
            longitude=self._lng,
            since_timestamp_ms=timestamps,
            cell_id=cell_ids)
        self._call()

        self.use_item_egg_incubator()

    @chain_api
    def catch_pokemon(self, pokemon):

        self._encounter(pokemon)
        ret = self._call()
        if ret[0]:

            max_cp = ret[1]
            pokemon_id = ret[2]

            ret = -1
            while ret == -1 or ret == 2 or ret == 4:
                if POKEDEX[pokemon_id]['EvolvesFrom']:
                    family_id = POKEDEX[POKEDEX[pokemon_id]['EvolvesFrom']]['PkMn']
                else:
                    family_id = pokemon_id

                if len(self.pokemon[pokemon_id]) == 0 or self.candy[family_id] < 100 or max_cp > 2000:
                    self.use_item_capture(pokemon)
                    if self.item[ItemId.Value('ITEM_ULTRA_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_ULTRA_BALL')
                    elif self.item[ItemId.Value('ITEM_GREAT_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_GREAT_BALL')
                    elif self.item[ItemId.Value('ITEM_POKE_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_POKE_BALL')
                    else:
                        log.warning('CATCH_POKEMON no balls!')
                        return
                else:
                    if self.item[ItemId.Value('ITEM_RAZZ_BERRY')] > 30:
                        self.use_item_capture(pokemon)

                    cnt_poke_ball = self.item[ItemId.Value('ITEM_POKE_BALL')]
                    cnt_great_ball = self.item[ItemId.Value('ITEM_GREAT_BALL')]
                    cnt_ultra_ball = self.item[ItemId.Value('ITEM_ULTRA_BALL')]

                    if cnt_ultra_ball > 100:
                        pokeball = ItemId.Value('ITEM_ULTRA_BALL')
                    elif cnt_great_ball > 0 and cnt_great_ball + cnt_ultra_ball > 100:
                        pokeball = ItemId.Value('ITEM_GREAT_BALL')
                    elif self.item[ItemId.Value('ITEM_POKE_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_POKE_BALL')
                    elif self.item[ItemId.Value('ITEM_GREAT_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_GREAT_BALL')
                    elif self.item[ItemId.Value('ITEM_ULTRA_BALL')] > 0:
                        pokeball = ItemId.Value('ITEM_ULTRA_BALL')
                    else:
                        log.warning('CATCH_POKEMON no balls!')
                        return

                self._catch_pokemon(pokeball, pokemon)
                ret = self._call()

    @chain_api
    def use_item_capture(self, pokemon):
        if self.item[ItemId.Value('ITEM_RAZZ_BERRY')] > 0:
            self._api.use_item_capture(
                item_id=ItemId.Value('ITEM_RAZZ_BERRY'),
                encounter_id=pokemon['encounter_id'],
                spawn_point_guid=pokemon['spawn_point_id'])
            if not self._call():
                self.use_item_capture(pokemon)
        else:
            log.info('USE_ITEM_CAPTURE, out of berry :(')

    def _encounter(self, pokemon):
        self._api.encounter(
            encounter_id=pokemon['encounter_id'],
            spawn_point_id=pokemon['spawn_point_id'],
            player_latitude=self._lat,
            player_longitude=self._lng)

    def _catch_pokemon(self, pokeball, pokemon):
        self._api.catch_pokemon(
            encounter_id=pokemon['encounter_id'],
            pokeball=pokeball,
            normalized_reticle_size=1.950,
            spawn_point_id=pokemon['spawn_point_id'],
            hit_pokemon=True,
            spin_modifier=1,
            normalized_hit_position=1)

    @chain_api
    def use_item_egg_incubator(self):
        for _,incubator in self.incubator.iteritems():
            if not 'pokemon_id' in incubator:
                for egg in self.egg:
                    if not 'egg_incubator_id' in egg:
                        self._use_item_egg_incubator(incubator['id'], egg['id'])
                        self._call()
                        break

    def _use_item_egg_incubator(self, item_id, pokemon_id):
        self._api.use_item_egg_incubator(item_id=item_id, pokemon_id=pokemon_id)

    # Spin the pokestop
    @chain_api
    def fort_search(self, pokestop):
        self._api.fort_search(
            fort_id=pokestop['id'],
            fort_latitude=pokestop['latitude'],
            fort_longitude=pokestop['longitude'],
            player_latitude=self._lat,
            player_longitude=self._lng)
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

    def _get_hatched_eggs(self):
        self._api.get_hatched_eggs()

    def _get_inventory(self):
        self.egg = []
        self.incubator = {}
        self.pokemon = defaultdict(list)
        self.candy = defaultdict(int)
        self.item = defaultdict(int)
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
