#!/usr/bin/env python3
"""Provision selected builds through native game interfaces; keep the lock through the match."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
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


def rpc(container, method, params=None, timeout=10):
    body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params or {}))
    data = json.loads(docker('exec', '--user', 'node', container, 'curl', '-fsS',
        '--max-time', str(timeout), '-H', 'Content-Type: application/json', '-d', body,
        'http://127.0.0.1:8086/rpc'))
    if 'error' in data:
        raise RuntimeError(method + ' failed: ' + data['error'].get('message', 'RPC error'))
    return data['result']


def process_running(container, name):
    return json.loads(docker('exec', container, 'sh', '-c',
        'pgrep -x "$1" >/dev/null && echo true || echo false', 'sh', name))


class Console:
    """Let Docker handle attachment and stream framing; send only native commands."""
    def __init__(self, container):
        self.process = subprocess.Popen(['docker', 'attach', '--sig-proxy=false', container],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def command(self, command):
        if '\n' in command or '\r' in command:
            raise ValueError('Console command must be one line')
        self.process.stdin.write((command + '\n').encode())
        self.process.stdin.flush()
        reply, deadline = b'', time.monotonic() + 30
        # Native console prints a prompt on queuing input and another on completion.
        while reply.count(b'AC> ') < 2:
            if not select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                raise RuntimeError('World console timed out')
            part = os.read(self.process.stdout.fileno(), 65536)
            if not part:
                raise RuntimeError('World console disconnected')
            reply += part
        return reply.decode(errors='replace')

    def close(self):
        self.process.terminate()  # --sig-proxy=false keeps the server running.
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()


class Player:
    def __init__(self, image, network, binary, credentials, name, build, existing=False):
        self.process = subprocess.Popen(['docker', 'run', '--rm', '-i', '--network', network,
            '--entrypoint', '/provision-client', '-v', str(binary) + ':/provision-client:ro', image],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        try:
            self.identity = self.call(dict(action='open' if existing else 'create', name=name, **credentials,
                race_id=build['race_id'], class_id=build['class_id'], appearance=build['appearance']))
        except Exception:
            self.close()
            raise

    def call(self, request):
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
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
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=8)


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


def verify_saved(console, world, name, path, build, dbc):
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
                path.with_suffix('.differences.json').unlink(missing_ok=True)
                return
        except (ValueError, KeyError) as error:
            differences = [{'error': str(error)}]
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


def locate(inventory, item_id, claimed):
    deadline = time.monotonic() + 5
    while True:
        found = next((i for i in inventory() if int(i['itemEntry']) == item_id
                      and i['bag'] == 0 and i['slot'] >= 23 and i['guid'] not in claimed), None)
        if found:
            claimed.add(found['guid'])  # Dumps may still contain an already consumed/equipped item.
            return found
        if time.monotonic() >= deadline:
            raise ValueError('Granted item missing: ' + str(item_id))
        time.sleep(0.2)


def teleport_to_arena(console, name):
    reply = console.command('tele name ' + name + ' GurubashiArena')
    if 'teleporting' not in reply.lower():
        raise RuntimeError('Gurubashi teleport was not confirmed for ' + name)


def apply_build(plan, player, console, world, dbc):
    build, name = plan['build'], player.identity['name']
    dump_path = STATE / (plan['client'] + '.sql')

    def snapshot():
        console.command('saveall')
        export(console, world, name, dump_path)
        return validator.tables(dump_path)

    def inventory():
        return read_inventory(snapshot)

    def grant(item_id, count=1):
        player.chat(f'additem {item_id} {count}')

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
    added_spells, claimed = set(), set()
    known_spells = set()
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
        instance = locate(inventory, item['item_id'], claimed)
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
            gem = locate(inventory, gem_id, claimed)
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
            instance = locate(inventory, glyph_item, claimed)
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


def provision(plan, credentials, console, world, image, network, binary, dbc):
    build = plan['build']
    record_path = STATE / (plan['client'] + '.json')
    digest = hashlib.sha256(json.dumps(build, sort_keys=True).encode()).hexdigest()
    old = json.loads(record_path.read_text()) if record_path.exists() else None
    reuse = old is not None and old['build_sha256'] == digest and old['account'] == plan['account']
    dump_path = STATE / (plan['client'] + '.sql')
    if reuse:
        verify_saved(console, world, old['name'], dump_path, build, dbc)
        console.command('character changeaccount ' + credentials['username'] + ' ' + old['name'])
        record = old
    else:
        name = 'Duel' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
        record = dict(status='provisioning', account=plan['account'], name=name,
                      build=plan['path'], build_sha256=digest)
        save(record_path, record)
    name = record['name']
    player, verified = None, False
    try:
        player = Player(image, network, binary, credentials, name, build, existing=reuse)
        if reuse and str(player.identity['guid']) != record['guid']:
            raise ValueError('The saved fighter identity changed')
        record['guid'] = str(player.identity['guid'])
        save(record_path, record)
        if not reuse:
            console.command('character level ' + name + ' 80')
        # Death Knights get their full talent budget outside Ebon Hold.
        teleport_to_arena(console, name)
        player.call(dict(action='enter'))
        player.chat('gm off')
        if not reuse:
            print(plan['client'] + ': applying build to ' + name, flush=True)
            apply_build(plan, player, console, world, dbc)
        console.command('revive ' + name)  # GM staging account: restore full native vitals.
        player.chat('cooldown')
        player.call(dict(action='logout'))
        verify_saved(console, world, name, dump_path, build, dbc)
        verified = True
    finally:
        if player:
            player.close()
        if reuse or verified:
            console.command('character changeaccount ' + plan['account'] + ' ' + name)
            if plan['account'] not in console.command('pinfo ' + name):
                raise RuntimeError('Character ownership transfer was not confirmed')
    record['status'] = 'complete'
    save(record_path, record)
    return record


def run(check=False, on_ready=None):
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
                if old.get('status') != 'complete':
                    raise ValueError('An incomplete provisioning attempt exists; inspect ' + str(previous))
        dbc = STATE / 'dbc'
        dbc.mkdir(exist_ok=True)
        for name in ('Talent', 'TalentTab', 'GlyphProperties', 'GlyphSlot', 'Spell', 'SpellItemEnchantment'):
            docker('cp', ids['worldserver'] + ':/azerothcore/env/dist/data/dbc/' + name + '.dbc', dbc / (name + '.dbc'))
        spells = validator.dbc_rows(dbc / 'Spell.dbc')
        for plan in plans:
            validator.validate(plan['build'])
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
        if check:
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
                    rpc(ids[client], 'session.logout', timeout=40)
                disconnected.append(ids[client])
            for plan in plans:
                record = provision(plan, admin, console, ids['worldserver'], image, network, binary, dbc)
                # Container volumes persist; startup will enter this verified character.
                target = STATE / (plan['client'] + '-character.json')
                save(target, record)
                target.chmod(0o644)
                docker('cp', target, ids[plan['client']] + ':/workspace/character.json')
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
        if on_ready:
            on_ready(ids, [json.loads((STATE / (client + '.json')).read_text()) for client in CLIENTS])


if __name__ == '__main__':
    raise SystemExit('Use python3 local/duel.py [--check] [--seconds 180]')
