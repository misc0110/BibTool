import re
import sys
import requests
import json
import hashlib
import bibtexparser
import difflib
import os
import argparse
import tempfile
from urllib.parse import quote, urlparse

BIBTEXPARSER_V2 = hasattr(bibtexparser, "parse_string")

if not BIBTEXPARSER_V2:
    from bibtexparser.bparser import BibTexParser
    from bibtexparser.bibdatabase import BibDatabase

    def bib_loads(text):
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        parser.homogenize_fields = True
        return bibtexparser.loads(text if text.strip() else "\n", parser)

    def bib_dumps(bib_db):
        return bibtexparser.dumps(bib_db)

    def entry_to_bibtex(entry):
        newdb = BibDatabase()
        newdb.entries = [ entry ]
        return bibtexparser.dumps(newdb)

else:
    from bibtexparser.model import Entry, Field, ParsingFailedBlock

    _COMMON_STRINGS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April",
                       "may": "May", "jun": "June", "jul": "July", "aug": "August",
                       "sep": "September", "oct": "October", "nov": "November", "dec": "December"}
    _FIELD_ALIASES = {"keyw": "keyword", "keywords": "keyword", "authors": "author",
                      "editors": "editor", "urls": "url", "link": "url", "links": "url",
                      "subjects": "subject", "xref": "crossref"}

    class BibDatabase(object):
        def __init__(self, entries=None, other_blocks=None):
            self.entries = entries if entries is not None else []
            self.other_blocks = other_blocks if other_blocks is not None else []

    def _entry_to_dict(block):
        entry = {}
        for field in block.fields:
            key = field.key.lower()
            key = _FIELD_ALIASES.get(key, key)
            value = field.value if isinstance(field.value, str) else str(field.value)
            if getattr(field, "enclosing", None) == "no-enclosing" and value.lower() in _COMMON_STRINGS:
                value = _COMMON_STRINGS[value.lower()]
            entry[key] = value
        entry["ENTRYTYPE"] = block.entry_type.lower()
        entry["ID"] = block.key
        return entry

    _MONTH_MACROS = {v.lower(): k for k, v in _COMMON_STRINGS.items()}
    _MONTH_MACROS.update({k: k for k in _COMMON_STRINGS})

    def _make_field(key, value):
        # write months as BibTeX month macros (month = jan) instead of {January}
        if key == "month" and isinstance(value, str) and value.strip().lower() in _MONTH_MACROS:
            try:
                return Field(key, _MONTH_MACROS[value.strip().lower()], enclosing="no-enclosing")
            except TypeError:
                pass  # older bibtexparser 2 betas cannot write macros, keep the braces
        return Field(key, value)

    def _dict_to_entry(entry):
        fields = [_make_field(k, entry[k]) for k in sorted(entry) if k not in ("ID", "ENTRYTYPE")]
        return Entry(entry.get("ENTRYTYPE", "misc"), entry["ID"], fields)

    def _write_block(block):
        bib_format = bibtexparser.BibtexFormat()
        bib_format.indent = " "
        bib_format.parsing_failed_comment = ""
        return bibtexparser.write_string(bibtexparser.Library([block]), bibtex_format=bib_format)

    def bib_loads(text):
        library = bibtexparser.parse_string(text)
        entries = []
        other = []
        for block in library.blocks:
            if isinstance(block, Entry):
                entries.append(_entry_to_dict(block))
            else:
                if isinstance(block, ParsingFailedBlock):
                    print("Warning: could not parse BibTeX block (kept unchanged): %s" % block.error)
                other.append(block)
        return BibDatabase(entries, other)

    def bib_dumps(bib_db):
        parts = [_write_block(b) for b in bib_db.other_blocks]
        parts += [entry_to_bibtex(e) for e in sorted(bib_db.entries, key=lambda e: e["ID"].lower())]
        return "\n".join(p.strip("\n") + "\n" for p in parts)

    def entry_to_bibtex(entry):
        return _write_block(_dict_to_entry(entry))


def bib_load(path):
    with open(path, encoding="utf-8") as bibtex_file:
        return bib_loads(bibtex_file.read())


def bib_dump(bib_db, path):
    # serialize before opening, opening truncates the file
    content = bib_dumps(bib_db)
    with open(path, "w", encoding="utf-8") as bibtex_file:
        bibtex_file.write(content)

version = 18

limit_traffic = True

parser = argparse.ArgumentParser(description='BibTool')
parser.add_argument("--token", dest="token", action="store", default="", help="Provide access token via command line")
parser.add_argument("--tokenfile", dest="token_file", action="store", default="token", help="File containing the access token")
parser.add_argument("--server", dest="server", action="store", default="", required=True, help="BibTool server")
parser.add_argument("--tex", dest="tex", action="store", default="main.tex", help="LaTeX file")
parser.add_argument("--query", dest="query", action="store", default="", help="Query to search for (if action is search)")
parser.add_argument("action")

args = parser.parse_args(sys.argv[1:])

if args.token != "":
    token = args.token
else:
    token = None
    try:
        token = open(args.token_file).read().strip()
    except:
        pass

fname = args.tex
server = args.server.strip()
if not server:
    print("Usage: %s --server <your bib server> <action>" % sys.argv[0])
    sys.exit(1)
if server[-1] != '/': server += "/"
if not server.endswith("/v1/"): server += "v1/"

CITE_RE = re.compile(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^\}]+)\}")
INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^\}]+)\}")
SUBIMPORT_RE = re.compile(r"\\subimport\*?\s*\{([^\}]*)\}\s*\{([^\}]*)\}")
COMMENT_RE = re.compile(r"(?<!\\)%.*")


def get_keys(filename, import_base=None, visited=None):
    if visited is None:
        visited = set()
    try:
        if not os.path.isfile(filename):
            filename += ".tex"
        real = os.path.realpath(filename)
        if real in visited:
            return []
        visited.add(real)
        content = open(filename, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print("Warning: could not read '%s': %s" % (filename, e.strerror))
        return []

    content = COMMENT_RE.sub("", content)

    # extract cites
    keys = set()
    for key in CITE_RE.findall(content):
        keys |= set(key.split(","))
    # find inputs/include and recursively parse them
    for f in INPUT_RE.findall(content):
        f = f.strip()
        if import_base is not None:
            f = os.path.join(import_base, f)
        keys |= set(get_keys(f, import_base, visited))

    # find subimports and recursively parse them
    for f in SUBIMPORT_RE.findall(content):
        base = f[0].strip()
        if import_base is not None:
            base = os.path.join(import_base, base)
        keys |= set(get_keys(os.path.join(base, f[1].strip()), import_base=base, visited=visited))

    keys = sorted(list([k.strip() for k in keys]))
    keys = [k for k in keys if len(k) != 0 and k != "*"]
    return keys


def keys_hash(keys):
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def keys_have_changed(keys):
    old_keys = ""
    try:
        old_keys = open("main.bib.keys.sha").read().strip()
    except:
        pass
    return (keys_hash(keys) != old_keys)


def save_keys_hash(keys):
    # only called after the keys were fetched successfully
    try:
        open("main.bib.keys.sha", "w").write(keys_hash(keys))
    except:
        pass


def bib_has_changed(bib):
    new_bib = hashlib.sha256(bib.strip().encode("utf-8")).hexdigest()
    old_bib = ""
    try:
        old_bib = open("main.bib.sha").read().strip()
    except:
        pass
    return (new_bib != old_bib)


def entry_by_key(key):
    for entry in bib_database.entries:
        if entry["ID"] == key:
            return entry
    return None


def inline_diff(a, b):
    matcher = difflib.SequenceMatcher(None, a, b)
    def process_tag(tag, i1, i2, j1, j2):
        if tag == 'replace':
            return '\u001b[34m[' + matcher.a[i1:i2] + ' -> ' + matcher.b[j1:j2] + ']\u001b[0m'
        if tag == 'delete':
            return '\u001b[31m[- ' + matcher.a[i1:i2] + ']\u001b[0m'
        if tag == 'equal':
            return matcher.a[i1:i2]
        if tag == 'insert':
            return '\u001b[32m[+ ' + matcher.b[j1:j2] + ']\u001b[0m'
    return ''.join(process_tag(*t) for t in matcher.get_opcodes())


def resolve_changes():
    print("Your options are")
    print("  update server version with local changes (L)")
    print("  replace local version with server version (S)")
    print("  ignore, do not apply any changes (I)")
    print("  abort without changes (A)")
    while True:
        action = input("Your choice [l/s/I/a]: ").lower()
        if action == "l" or action == "s" or action == "a" or action == "i":
            return action
        if not action or action == "":
            return "i"
    return None


def resolve_duplicate():
    print("Your options are")
    print("  commit local changes to server (M)")
    print("  delete server entry (D)")
    print("  remove local entry (R)")
    print("  ignore, do not apply any changes (I)")
    print("  abort without changes (A)")
    while True:
        action = input("Your choice [m/d/r/I/a]: ").lower()
        if action == "m" or action == "a" or action == "i" or action == "d" or action == "r":
            return action
        if not action or action == "":
            return "i"
    return None

def resolve_policy_reject():
    print("Your options are")
    print("  force entry write to the server (F)")
    print("  ignore, do not apply any changes (I)")
    print("  abort without changes (A)")
    while True:
        action = input("Your choice [f/I/a]: ").lower()
        if action == "f" or action == "i" or action == "a":
            return action
        if not action or action == "":
            return "i"
    return None


def update_local_bib(key, new_entry):
    for (idx, entry) in enumerate(bib_database.entries):
        if entry["ID"] == key:
            bib_database.entries[idx] = new_entry
            break


def url_part(value):
    # keys and queries must not change the URL structure
    return quote(value, safe="")


def token_suffix():
    return "/%s" % url_part(token) if token else ""


def json_response(response):
    try:
        result = response.json()
    except ValueError:
        print("\u001b[31m[!] Invalid response from server\u001b[0m (HTTP %d): %s" % (response.status_code, response.text[:200]))
        sys.exit(1)
    if response.status_code >= 400 and not (isinstance(result, dict) and "reason" in result):
        print("\u001b[31m[!] Server returned HTTP %d\u001b[0m" % response.status_code)
        sys.exit(1)
    return result


def update_remote_bib(key, new_entry):
    result = json_response(requests.put(server + "entry/%s" % url_part(key), json = {"entry": new_entry, "token": token}))
    if "success" in result and not result["success"]:
        show_error(result)

def add_remote_bib(key, entry, force=False):
    if force:
        # do not rely on boolean encoding of `force`
        response = requests.post(server + "entry/%s" % url_part(key), json = {"entry": entry, "token": token, "force": "true"})
    else:
        response = requests.post(server + "entry/%s" % url_part(key), json = {"entry": entry, "token": token})
    result = json_response(response)
    if "success" in result and not result["success"]:
        show_error(result)

def remove_remote_bib(key):
    result = json_response(requests.delete(server + "entry/%s%s" % (url_part(key), token_suffix())))
    if "success" in result and not result["success"]:
        show_error(result)


def remove_local_bib(key):
    bib_database.entries = [e for e in bib_database.entries if e["ID"] != key]
    save_bib()


def save_bib_hash():
    try:
        bib = open("main.bib").read()
        open("main.bib.sha", "w").write(hashlib.sha256(bib.strip().encode("utf-8")).hexdigest())
    except:
        pass


def save_bib():
    bib_dump(bib_database, 'main.bib')


def show_error(obj):
    if "reason" in obj:
        if obj["reason"] == "access_denied":
            print("\u001b[31m[!] Access denied!\u001b[0m Your token is not valid for this operation. Verify whether the file '%s' contains a valid token." % args.token_file)
        elif obj["reason"] == "policy":
            for entry in obj["entries"]:
                print("\u001b[31m[!] Server policy rejected entry %s\u001b[0m. Reason: %s" % (entry["ID"], entry["reason"]))
        elif obj["reason"] == "invalid_entry":
            print("\u001b[31m[!] Server rejected malformed entry:\u001b[0m %s Fix it in main.bib and run again." % obj.get("message", ""))
        else:
            print("\u001b[31m[!] Unhandled error occurred!\u001b[0m Reason (%s) %s" % (obj["reason"], obj["message"] if "message" in obj else ""))
    else:
        print("\u001b[31m[!] Unknown error occurred!\u001b[0m")
    sys.exit(1)

action = args.action

if not os.path.exists("main.bib") or os.stat("main.bib").st_size == 0:
    bib_database = bib_loads("")
else:
    try:
        bib_database = bib_load('main.bib')
    except Exception as e:
        print("Malformed bibliography file!\n")
        print(e)
        sys.exit(1)

def self_update(version_info):
    url = version_info.get("url", "client.py")
    if not isinstance(url, str) or not re.match(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-./]*$", url) or ".." in url:
        print("\u001b[31m[!] Server sent an invalid update URL, not updating.\u001b[0m")
        return False
    if urlparse(server).scheme != "https":
        print("\u001b[33m[!] New client version available, but the server is not using HTTPS.\u001b[0m "
              "Not updating automatically, please download %s%s manually and verify it." % (server, url))
        return False
    print("[!] New version available, updating...")
    try:
        script = requests.get(server + url, timeout=30)
    except requests.RequestException as e:
        print("\u001b[31m[!] Could not download update:\u001b[0m %s" % e)
        return False
    if script.status_code != 200:
        print("\u001b[31m[!] Could not download update\u001b[0m (HTTP %d)" % script.status_code)
        return False
    code = script.text
    # make sure we got a valid client of the announced version before replacing ourselves
    try:
        compile(code, "client.py", "exec")
    except SyntaxError:
        print("\u001b[31m[!] Downloaded update is not valid Python, not updating.\u001b[0m")
        return False
    if not re.search(r"^version = %d$" % int(version_info["version"]), code, re.MULTILINE):
        print("\u001b[31m[!] Downloaded update has an unexpected version, not updating.\u001b[0m")
        return False
    target = os.path.realpath(sys.argv[0])
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".py")
    try:
        with os.fdopen(fd, "w") as sc:
            sc.write(code)
        try:
            os.chmod(tmp, os.stat(target).st_mode)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError as e:
        print("\u001b[31m[!] Could not write update:\u001b[0m %s" % e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


try:
    response = requests.get(server + "version")
    version_info = response.json()
    int(version_info["version"])
except Exception:
    print("\u001b[31m[!] Could not get version info from server.\u001b[0m Is the server URL \"%s\" correct?" % server)
    sys.exit(1)

if int(version_info["version"]) > version:
    if self_update(version_info):
        print("Restarting...")
        os.execl(sys.executable, *([sys.executable]+sys.argv))


if action == "search":
    if len(args.query) < 3:
        print("Usage: %s search --query <query>" % sys.argv[0])
        sys.exit(1)
    response = requests.get(server + "search/" + url_part(args.query) + token_suffix())
    print(response.text)

elif action == "sync":
    response = requests.get(server + "sync" + token_suffix())
    print(response.text)

elif action == "get":
    keys = get_keys(args.tex)
    if not keys:
        print("Warning: no citations found in '%s'" % args.tex)
    # also fetch if a cited entry is missing locally, regardless of the stored hashes
    fetch = keys_have_changed(keys) or any(not entry_by_key(k) for k in keys if '#' not in k)
    try:
        current_bib = open("main.bib").read()
        update = bib_has_changed(current_bib)
    except:
        update = False
        fetch = True

    if update:
        fetch = True
    if not limit_traffic:
        update = True
        fetch = True
    #print("fetch %d, update %d\n" % (fetch, update))

    # update
    if update:
        result = json_response(requests.post(server + "update", json = {"entries": bib_database.entries, "token": token}))
        if not result["success"]:
            if result["reason"] == "policy":
                #print(result["entries"])
                for entry in result["entries"]:
                    print("\n[!] Server policy rejected entry %s. Reason: %s" % (entry["ID"], entry["reason"]))
                    action = resolve_policy_reject()
                    if action == "i":
                        pass
                    elif action == "a":
                        sys.exit(1)
                    elif action == "f":
                        add_remote_bib(entry["ID"], entry_by_key(entry["ID"]), force=True)
            elif result["reason"] == "duplicate":
                #print(result["entries"])
                for dup in result["entries"]:
                    print("\n[!] There is already a similar entry for %s on the server (%s) [Levenshtein %d]" % (dup[1], dup[2]["ID"], dup[0]))
                    print("- Local -")
                    local = entry_to_bibtex(entry_by_key(dup[1]))
                    remote = entry_to_bibtex(dup[2])
                    print(local)
                    print("- Server -")
                    print(remote)
                    print("- Diff - ")
                    print(inline_diff(remote, local))

                    if dup[1] != dup[2]["ID"]:
                        # different key, similar entry
                        action = resolve_duplicate()
                        if action == "i":
                            pass
                        elif action == "a":
                            sys.exit(1)
                        elif action == "d":
                            remove_remote_bib(dup[2]["ID"])
                        elif action == "m":
                            add_remote_bib(dup[1], entry_by_key(dup[1]))
                        elif action == "r":
                            remove_local_bib(dup[1])
                    else:
                        # same key
                        action = resolve_changes()
                        if action == "a":
                            sys.exit(1)
                        elif action == "i":
                            pass
                        elif action == "s":
                            update_local_bib(dup[1], dup[2])
                            save_bib()
                        elif action == "l":
                            update_remote_bib(dup[2]["ID"], entry_by_key(dup[1]))
            else:
                show_error(result)

    if fetch:
        bib = json_response(requests.post(server + "get_json", json = {"entries": keys, "token": token}))
        if isinstance(bib, dict) and "success" in bib and not bib["success"]:
            show_error(bib)
        else:
            # merge local and remote database
            for entry in bib:
                if entry and "ID" in entry and not entry_by_key(entry["ID"]):
                    bib_database.entries.append(entry)
            save_bib()
            # only remember this state after everything succeeded
            save_bib_hash()
            save_keys_hash(keys)

            # suggest keys for unresolved keys
            for key in keys:
                if not entry_by_key(key) and not '#' in key:
                    suggest = json_response(requests.get(server + "suggest/" + url_part(key) + token_suffix()))
                    if "success" in suggest and not suggest["success"]:
                        show_error(suggest)
                    else:
                        print("Key '%s' not found%s %s" % (key, ", did you mean any of these?" if len(suggest["entries"]) > 0 else "", ", ".join(["'%s'" % e[1]["ID"] for e in suggest["entries"]])))

else:
    print("Unknown action '%s'" % action)
