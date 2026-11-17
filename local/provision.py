#!/usr/bin/env python3
"""After agents finish, provision next-match.json selections and reconnect both game clients.

Run python3 local/provision.py [--check]. Existing characters are kept.
"""
import argparse
from collections import defaultdict
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time

import build as validator

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'tmp/local/provision'
CLIENTS = ('client-1', 'client-2')
SELECT = """import json,os
from pathlib import Path
r=Path('/workspace').resolve();s=json.loads((r/'next-match.json').read_text())
if not isinstance(s,dict) or set(s)!={'build'} or not isinstance(s['build'],str):raise ValueError('next-match.json needs a build path')
p=(r/s['build']).resolve();catalog=r/'builds'
if not p.is_relative_to(catalog) or p.name!='build.json':raise ValueError('Select a supplied builds/<name>/build.json')
i=json.loads((catalog/'index.json').read_text())
entry=next(e for e in i['builds'] if e['build']==str(p.relative_to(catalog)))
print(json.dumps({'path':str(p.relative_to(r)),'build':json.loads(p.read_text()),'source':entry,'account':os.environ['WOW_ACCOUNT']}))
"""


def docker(*args, data=None, timeout=90):
    result = subprocess.run(['docker', *map(str, args)], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        # Never include Docker output: commands and container logs can contain credentials.
        raise RuntimeError('Docker operation failed: ' + str(args[0]))
    return result.stdout


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def process_running(container, name):
    return json.loads(docker('exec', container, 'sh', '-c',
        'pgrep -x "$1" >/dev/null && echo true || echo false', 'sh', name))


class Console:
    """Use Docker's native attach API to send supported worldserver commands."""
    def __init__(self, container):
        endpoint = docker('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}').decode().strip()
        if not endpoint.startswith('unix://'):
            raise ValueError('Provisioning supports only the local Docker UNIX socket')
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(endpoint[7:])
        request = ('POST /containers/' + container + '/attach?stream=1&stdin=1&stdout=1&stderr=1 HTTP/1.1\r\n'
                   'Host: localhost\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n')
        self.sock.sendall(request.encode())
        head = b''
        while not head.endswith(b'\r\n\r\n'):
            head += self.read(1)
            if len(head) > 8192:
                raise RuntimeError('Invalid Docker attach response')
        if not head.startswith(b'HTTP/1.1 101'):
            raise RuntimeError('Docker did not establish console attachment')

    def read(self, count):
        value = b''
        while len(value) < count:
            part = self.sock.recv(count - len(value))
            if not part:
                raise RuntimeError('World console disconnected')
            value += part
        return value

    def command(self, command):
        if '\n' in command or '\r' in command:
            raise ValueError('Console command must be one line')
        self.sock.sendall((command + '\n').encode())
        reply = b''
        # This server prints a prompt when queuing input and another on completion.
        while reply.count(b'AC> ') < 2:
            header = self.read(8)
            length = int.from_bytes(header[4:], 'big')
            if length > 1 << 20:
                raise RuntimeError('Invalid console frame')
            reply += self.read(length)
        return reply.decode(errors='replace')

    def close(self):
        self.sock.close()


class Player:
    def __init__(self, image, network, binary, credentials, name, build, existing=False):
        self.process = subprocess.Popen(['docker', 'run', '--rm', '-i', '--network', network,
            '--entrypoint', '/provision-client', '-v', str(binary) + ':/provision-client:ro', image],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.identity = self.call(dict(action='open' if existing else 'create', name=name, **credentials,
            race_id=build['race_id'], class_id=build['class_id'], appearance=build['appearance']))

    def call(self, request):
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
        import select
        if not select.select([self.process.stdout], [], [], 50)[0]:
            raise RuntimeError('Provisioning client timed out')
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('Provisioning client disconnected')
        result = json.loads(line)
        if not result.get('ok'):
            step = request['action'] + (' ' + request['command'].split()[0] if request['action'] == 'chat' else '')
            raise RuntimeError('Provisioning ' + step + ' failed: ' + result.get('error', 'Client failed'))
        return result

    def chat(self, command):
        return self.call(dict(action='chat', command=command))

    def packet(self, opcode, data, spell=0):
        return self.call(dict(action='packet', opcode=opcode, payload=data.hex(), spell=spell))

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=8)


def tables(path, validator):
    result = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line or line.startswith('IMPORTANT NOTE:'):
            continue
        match = validator.INSERT.fullmatch(line)
        if not match:
            raise ValueError('Unexpected native character dump format')
        table, columns, values = match.groups()
        values = next(csv.reader([values], quotechar="'", escapechar='\\', skipinitialspace=True))
        result[table].append(dict(zip(re.findall(r'`(\w+)`', columns), values)))
    return result


def export(console, world, name, path):
    # Native pdump forbids paths. Export in the server's working directory.
    filename = 'duel-build-' + secrets.token_hex(8) + '.sql'
    directory = docker('inspect', '--format', '{{.Config.WorkingDir}}', world).decode().strip() or '/'
    remote = directory.rstrip('/') + '/' + filename
    console.command('pdump write ' + filename + ' ' + name)
    try:
        docker('cp', world + ':' + remote, path)
    finally:
        docker('exec', world, 'rm', '-f', '--', remote)


def verify_saved(console, world, name, path, build, validator, dbc):
    # Saving is asynchronous; only retry reads, never a mutation.
    deadline = time.monotonic() + 10
    differences = []
    while True:
        export(console, world, name, path)
        try:
            observed = validator.observed_build(path, name)
            validator.check_specializations(observed, dbc, from_dump=True)
            # Native default secondary skills (e.g. DK First Aid) cannot be unlearned by clients.
            # Compare them only when the build explicitly selects them; primary skills remain exact.
            for skill in list(observed.get('professions', {})):
                if int(skill) not in validator.PRIMARY and skill not in build['professions']:
                    del observed['professions'][skill]
            differences = list(validator.differences(validator.normalize(build), validator.normalize(observed)))
            if not differences:
                return
        except (ValueError, KeyError):
            pass
        if time.monotonic() >= deadline:
            save(path.with_suffix('.differences.json'), differences)
            raise ValueError('Saved character did not match the selected build; inspect ' + str(path))
        time.sleep(0.2)


def packed(guid):
    octets = guid.to_bytes(8, 'little')
    return bytes([sum(1 << i for i, b in enumerate(octets) if b)]) + bytes(b for b in octets if b)


def read_inventory(snapshot):
    deadline = time.monotonic() + 5
    while True:
        data = snapshot()
        instances = {i['guid']: i for i in data['item_instance']}
        if all(i['item'] in instances for i in data['character_inventory']):
            return [dict(instances[i['item']], bag=int(i['bag']), slot=int(i['slot'])) for i in data['character_inventory']]
        if time.monotonic() >= deadline:
            raise RuntimeError('The native inventory save did not settle')
        time.sleep(0.1)


def teleport_to_arena(console, name):
    reply = console.command('tele name ' + name + ' GurubashiArena')
    if 'teleporting' not in reply.lower():
        raise RuntimeError('Gurubashi teleport was not confirmed for ' + name)


def provision(plan, credentials, console, world, image, network, binary, validator, dbc, allow_resume=False):
    build = plan['build']
    record_path = STATE / (plan['client'] + '.json')
    digest = hashlib.sha256(json.dumps(build, sort_keys=True).encode()).hexdigest()
    resume = None
    if record_path.exists():
        old = json.loads(record_path.read_text())
        if old.get('status') != 'complete':
            if (old.get('status') != 'provisioning' or old.get('stage') not in ('created', 'applying')
                    or (old['stage'] != 'created' and not allow_resume)
                    or old['build_sha256'] != digest or old['account'] != plan['account']):
                raise ValueError(plan['client'] + ': an incomplete attempt exists; inspect ' + str(record_path))
            resume = old
        elif old['build_sha256'] == digest and old['account'] == plan['account']:
            path = STATE / (plan['client'] + '-reuse.sql')
            verify_saved(console, world, old['name'], path, build, validator, dbc)
            teleport_to_arena(console, old['name'])
            # Re-stage through the owned GM account so an earlier duel cannot leave one fighter injured.
            console.command('character changeaccount ' + credentials['username'] + ' ' + old['name'])
            player = None
            try:
                player = Player(image, network, binary, credentials, old['name'], build, existing=True)
                if str(player.identity['guid']) != old['guid']:
                    raise ValueError('The saved fighter identity changed')
                player.call(dict(action='enter'))
                player.chat('gm off')
                console.command('revive ' + old['name'])  # GM account: full native health, mana and energy.
                player.chat('cooldown')
                player.call(dict(action='logout'))
            finally:
                if player:
                    player.close()
                console.command('character changeaccount ' + plan['account'] + ' ' + old['name'])
            return old
    name = resume['name'] if resume else 'Duel' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
    record = resume or dict(status='provisioning', account=plan['account'], name=name, build=plan['path'], build_sha256=digest)
    save(record_path, record)
    player = Player(image, network, binary, credentials, name, build, existing=bool(resume))
    if resume and str(player.identity['guid']) != resume['guid']:
        player.close()
        raise ValueError('The unfinished character changed; refusing to apply its build')
    record['guid'] = str(player.identity['guid'])
    record['stage'] = 'created'
    save(record_path, record)
    dump_path = STATE / (plan['client'] + '.sql')

    def snapshot():
        console.command('saveall')
        export(console, world, name, dump_path)
        return tables(dump_path, validator)

    def inventory():
        return read_inventory(snapshot)

    def grant(item_id, count=1):
        player.chat(f'additem {item_id} {count}')

    def locate(item_id):
        deadline = time.monotonic() + 5
        while True:
            found = next((i for i in inventory() if int(i['itemEntry']) == item_id and i['bag'] == 0 and i['slot'] >= 23), None)
            if found or time.monotonic() >= deadline:
                return found
            time.sleep(0.2)

    def item_guid(item):
        return (0x4000 << 48) | int(item['guid'])

    def cast_item(spell, target):
        row = spell_rows[spell]
        if spell not in known_spells:
            player.chat('learn ' + str(spell))
            added_spells.add(spell)
        for item_id, count in zip(row[52:60], row[60:68]):
            if 0 < item_id < 2**31 and count:
                grant(item_id, count)
        for item_id in row[50:52]:
            if item_id:
                grant(item_id)
        if row[18] == 1552:
            # Temporary native Runeforge (spell focus 1552); never saved as a world spawn.
            player.chat('gobject add temp 191758 10')
        player.packet(0x12e, struct.pack('<BIBI', 1, spell, 0, 0x10) + packed(target), spell)

    spell_rows = validator.dbc_rows(dbc / 'Spell.dbc')
    talents = validator.dbc_rows(dbc / 'Talent.dbc')
    enchantments = validator.dbc_rows(dbc / 'SpellItemEnchantment.dbc')
    added_spells = set()
    known_spells = set()
    try:
        console.command('character level ' + name + ' 80')
        # Death Knights get their full level-based talent budget outside Ebon Hold.
        teleport_to_arena(console, name)
        player.call(dict(action='enter'))
        record['stage'] = 'applying'
        save(record_path, record)
        print(plan['client'] + ': applying build to ' + name, flush=True)
        player.chat('gm off')
        # Learn class abilities, but never the GM command that grants every talent.
        player.chat('learn all my trainer')
        for item in inventory():
            grant(int(item['itemEntry']), -int(item['count']))
        # The staging character crafts its enchants; remove this extra skill before handoff.
        crafting_skills = {'333': 450, **build['professions']}
        for skill in map(int, crafting_skills):
            training = [r for r in spell_rows.values() if any(e == 44 and r[110+i] == skill for i,e in enumerate(r[71:74]))]
            if not training:
                raise ValueError('No profession training spell for ' + str(skill))
            row = max(training, key=lambda r: (max(r[80+i] for i,e in enumerate(r[71:74]) if e == 44 and r[110+i] == skill), -r[0]))
            player.chat('cast self ' + str(row[0]) + ' triggered')
            rank = crafting_skills[str(skill)]
            player.chat(f'setskill {skill} {rank} {rank}')
        rank_map = {spell: (row, rank) for row in talents.values() for rank, spell in enumerate(row[4:9]) if spell}
        for spell in sorted(build['specializations'][0]['talent_rank_spell_ids'], key=lambda s: (rank_map[s][0][2], rank_map[s][0][3])):
            row, rank = rank_map[spell]
            player.packet(0x251, struct.pack('<II', row[0], rank))
        player.chat('learn all my trainer')
        player.chat('maxskill')
        known_spells.update(int(row['spell']) for row in snapshot()['character_spell'])
        grant(44452)  # Runed Titanium Rod: covers the guides' enchanting tool categories.
        grant(40772)  # Gnomish Army Knife: engineering tools. Both are removed before handoff.
        reputation = {}
        for item in build['equipment'].values():
            if item:
                requirements = player.call(dict(action='item', item_id=item['item_id']))
                faction, rank = requirements['reputation_faction'], requirements['reputation_rank']
                if faction and rank:
                    reputation[faction] = max(rank, reputation.get(faction, 0))
        for faction, rank in reputation.items():
            standing = (-42000, -6000, -3000, 1, 3000, 9000, 21000, 42000)[rank]
            player.chat(f'modify reputation {faction} {standing}')
        equipped = {}
        for slot, item in build['equipment'].items():
            if item is None:
                continue
            grant(item['item_id'])
            instance = locate(item['item_id'])
            if instance is None:
                raise ValueError('Granted item missing: ' + str(item['item_id']))
            equipped[slot] = item_guid(instance)
            player.packet(0x10f, struct.pack('<QB', equipped[slot], validator.SLOTS.index(slot)))
        print(plan['client'] + ': equipment applied', flush=True)
        for slot, enchant in plan['source']['enchants'].items():
            cast_item(enchant['application_spell_id'], equipped[slot])
        print(plan['client'] + ': enchants applied', flush=True)
        for slot, item in build['equipment'].items():
            if item is None:
                continue
            for index, enchant_id in enumerate(item['gem_enchant_ids']):
                if not enchant_id:
                    continue
                gem_id = enchantments[enchant_id][33]
                grant(gem_id)
                gem = locate(gem_id)
                if gem is None:
                    raise ValueError('Granted gem missing: ' + str(gem_id))
                gems = [0, 0, 0]
                gems[index] = item_guid(gem)
                player.packet(0x347, struct.pack('<4Q', equipped[slot], *gems))
        print(plan['client'] + ': gems applied', flush=True)
        glyph_slots = validator.dbc_rows(dbc / 'GlyphSlot.dbc')
        slot_flags = {r[2] - 1: r[1] for r in glyph_slots.values() if 1 <= r[2] <= 6}
        for kind, flag in [('major', 0), ('minor', 1)]:
            for slot, refs in zip([i for i in range(6) if slot_flags[i] == flag], plan['source']['glyph_choices'][kind]):
                ref = refs[0]
                glyph_item = ref['id'] if ref['kind'] == 'item' else next(spell_rows[ref['id']][107+i] for i,e in enumerate(spell_rows[ref['id']][71:74]) if e == 24)
                glyph_id = build['specializations'][0]['glyph_ids'][slot]
                glyph_spell = next(r[0] for r in spell_rows.values() for i,e in enumerate(r[71:74]) if e == 74 and r[110+i] == glyph_id)
                grant(glyph_item)
                instance = locate(glyph_item)
                if instance is None:
                    raise ValueError('Granted glyph missing')
                player.packet(0xab, struct.pack('<BBB I Q I B I', 255, instance['slot'], 1, glyph_spell, item_guid(instance), slot, 0, 0), glyph_spell)
        print(plan['client'] + ': glyphs applied', flush=True)
        for row in snapshot()['character_skills']:
            if int(row['skill']) in validator.PROFESSIONS and row['skill'] not in build['professions']:
                player.packet(0x202, struct.pack('<I', int(row['skill'])))
        for spell in added_spells:
            player.chat('unlearn ' + str(spell))
        # Remove provisioning reagents/tools; never remove equipped items.
        for item in inventory():
            if item['bag'] == 0 and item['slot'] >= 23:
                grant(int(item['itemEntry']), -int(item['count']))
        for item in build['inventory']:
            grant(item['item_id'], item['count'])
        if build['ammo_id']:
            player.packet(0x268, struct.pack('<I', build['ammo_id']))
        console.command('revive ' + name)
        player.chat('cooldown')
        player.call(dict(action='logout'))
        verify_saved(console, world, name, dump_path, build, validator, dbc)
        console.command('character changeaccount ' + plan['account'] + ' ' + name)
        if plan['account'] not in console.command('pinfo ' + name):
            raise RuntimeError('Character ownership transfer was not confirmed')
        record['status'] = 'complete'
        record['stage'] = 'complete'
        save(record_path, record)
        return record
    finally:
        player.close()


def main(argv=None, on_ready=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate both selections without creating characters or stopping clients')
    parser.add_argument('--resume', action='store_true', help='Resume unfinished same-build characters using their existing names')
    args = parser.parse_args(argv)
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        compose = ['compose', '--env-file', ROOT / 'tmp/local/.env', '-f', ROOT / 'local/compose.yaml']
        ids = {service: docker(*compose, 'ps', '-q', service).decode().strip() for service in (*CLIENTS, 'worldserver')}
        if not all(ids.values()):
            raise ValueError('Start the local server and both clients with local/run.py up first')
        plans, errors = [], []
        for service in CLIENTS:
            try:
                plan = json.loads(docker('exec', '--user', 'node', ids[service], 'python3', '-c', SELECT))
                plan['client'] = service
                plans.append(plan)
            except (RuntimeError, ValueError):
                errors.append(service + ': select an existing builds/<name>/build.json in /workspace/next-match.json')
        if errors:
            raise ValueError('\n'.join(errors))
        for plan in plans:
            previous = STATE / (plan['client'] + '.json')
            if previous.exists():
                old = json.loads(previous.read_text())
                if old.get('status') != 'complete' and old.get('stage') != 'created' and not args.resume:
                    raise ValueError('An incomplete provisioning attempt exists; inspect ' + str(previous))
        dbc = STATE / 'dbc'
        dbc.mkdir(exist_ok=True)
        for name in ('Talent', 'TalentTab', 'GlyphProperties', 'GlyphSlot', 'Spell', 'SpellItemEnchantment'):
            docker('cp', ids['worldserver'] + ':/azerothcore/env/dist/data/dbc/' + name + '.dbc', dbc / (name + '.dbc'))
        spells = validator.dbc_rows(dbc / 'Spell.dbc')
        for plan in plans:
            path = STATE / (plan['client'] + '-build.json')
            save(path, plan['build'])
            validator.read_build(path)
            validator.check_specializations(plan['build'], dbc)
            if len(plan['build']['specializations']) != 1:
                raise ValueError('This provisioner supports the supplied single-specialization builds')
            all_items = [i for i in plan['build']['equipment'].values() if i] + plan['build']['inventory']
            if any(i['random_property_id'] for i in all_items):
                raise ValueError('Random-property items are not supported by the supplied build provisioner')
            if any(i['enchant_id'] or i['socket_enchant_id'] or any(i['gem_enchant_ids']) for i in plan['build']['inventory']):
                raise ValueError('Enchanted inventory items are not supported; select a supplied build')
            if not re.fullmatch(r'[A-Z0-9]{1,17}', plan['account']):
                raise ValueError('Unexpected local player account name')
            for slot, data in plan['source']['enchants'].items():
                item = plan['build']['equipment'].get(slot)
                spell_id = data['application_spell_id']
                row = spells.get(spell_id) if type(spell_id) is int else None
                expected = item['socket_enchant_id' if slot == 'waist' else 'enchant_id'] if item else 0
                if not row or not expected or not any(effect in (53, 156) and row[110+i] == expected for i,effect in enumerate(row[71:74])):
                    raise ValueError('Source enchant metadata does not match the selected build')
                if row[18] not in (0, 1552):
                    raise ValueError('Unsupported crafting spell focus in selected build')
            for choices in plan['source']['glyph_choices'].values():
                for refs in choices:
                    if not refs or any(r['kind'] not in ('item', 'spell') or type(r['id']) is not int or r['id'] <= 0 for r in refs):
                        raise ValueError('Invalid source glyph reference')
        print(json.dumps([dict(client=p['client'], build=p['path'], account=p['account']) for p in plans], indent=2), flush=True)
        if args.check:
            return
        for client in CLIENTS:
            if process_running(ids[client], 'codex'):
                raise ValueError('Finish and exit Codex in ' + client + ' before provisioning')
        image = docker('inspect', '--format', '{{.Config.Image}}', ids['client-1']).decode().strip()
        networks = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Networks}}', ids['client-1']))
        if len(networks) != 1:
            raise ValueError('Expected exactly one private game network')
        network = next(iter(networks))
        binary = STATE / 'client'
        docker('run', '--rm', '--network', network, '--entrypoint', 'go',
               '-e', 'GOMODCACHE=/output/go-mod', '-e', 'GOCACHE=/output/go-build', '-v', str(ROOT / 'local') + ':/input:ro',
               '-v', str(STATE) + ':/output', '-w', '/workspace/agent-wow', image, 'build', '-o', '/output/client', '/input/provision.go', timeout=300)
        console = Console(ids['worldserver'])
        admin_path = STATE / 'admin.json'
        initialized = admin_path.exists()
        admin = json.loads(admin_path.read_text()) if initialized else dict(username='BLD' + secrets.token_hex(5).upper(), password=secrets.token_hex(8))
        owned, disconnected = initialized, []
        try:
            if not initialized:
                reply = console.command('account create ' + admin['username'] + ' ' + admin['password'])
                if 'Account created' not in reply:
                    raise RuntimeError('The server did not confirm provisioning account creation')
                save(admin_path, admin)
                owned = True
            console.command('account set gmlevel ' + admin['username'] + ' 3 -1')
            for client in CLIENTS:
                if process_running(ids[client], 'agent-wow'):
                    print(client + ': logging out its game session', flush=True)
                    body = json.dumps(dict(jsonrpc='2.0', id=1, method='session.logout'))
                    reply = json.loads(docker('exec', ids[client], 'curl', '-fsS', '--max-time', '40',
                        '-H', 'Content-Type: application/json', '-d', body, 'http://127.0.0.1:8086/rpc'))
                    if 'error' in reply:
                        raise RuntimeError(client + ': game logout was rejected')
                disconnected.append(ids[client])
            for plan in plans:
                record = provision(plan, admin, console, ids['worldserver'], image, network, binary, validator, dbc, args.resume)
                # Container volumes persist; startup will enter this verified character.
                target = STATE / (plan['client'] + '-character.json')
                save(target, record)
                target.chmod(0o644)
                docker('cp', target, ids[plan['client']] + ':/workspace/character.json')
                docker('cp', ROOT / 'local/client.py', ids[plan['client']] + ':/usr/local/bin/local-client')
                print(plan['client'] + ': ' + record['name'] + ' provisioned and verified', flush=True)
        finally:
            try:
                if owned:
                    console.command('account set gmlevel ' + admin['username'] + ' 0 -1')
            finally:
                console.close()
                for container in disconnected:
                    if not process_running(container, 'agent-wow'):
                        docker('exec', '-d', '--user', 'node', '--workdir', '/workspace', container, '/usr/local/bin/local-client')
        for plan in plans:
            record = json.loads((STATE / (plan['client'] + '.json')).read_text())
            deadline = time.monotonic() + 90
            probe = """import json,re
from pathlib import Path
p=Path('/workspace/logs/session.log');text=p.read_text() if p.exists() else ''
lines=[l for l in text.splitlines() if 'world session ready' in l]
m=re.search(r'character.name=([^ ]+)',lines[-1]) if lines else None
g=re.search(r'character.guid=([^ ]+)',lines[-1]) if lines else None
print(json.dumps({'name':m.group(1) if m else None,'guid':g.group(1) if g else None}))
"""
            while time.monotonic() < deadline:
                try:
                    ready = json.loads(docker('exec', ids[plan['client']], 'python3', '-c', probe))
                    if ready['name'] == record['name'] and ready['guid'] == record['guid']:
                        print(plan['client'] + ': connected as ' + record['name'], flush=True)
                        break
                except RuntimeError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError(plan['client'] + ': character was provisioned but client startup was not confirmed')
        if on_ready:
            on_ready(ids, [json.loads((STATE / (client + '.json')).read_text()) for client in CLIENTS])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, StopIteration) as error:
        raise SystemExit(str(error) or 'Build provisioning could not complete')
