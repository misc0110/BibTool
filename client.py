import re
import sys
import requests
import json
import hashlib
import bibtexparser
from bibtexparser.bparser import BibTexParser
import difflib
import os
import argparse

version = 11

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
server = args.server
if server[-1] != '/': server += "/"
if not server.endswith("/v1/"): server += "v1/"

def get_keys(filename, import_base=None):
    content = open(filename).read()

    # extract cites
    keys = set()
    cites = re.findall("\\\\citeA?\\{([^\\}]+)\\}", content)
    for key in cites:
        keys |= set(key.split(","))

    # find inputs/include and recursively parse them
    inputs = re.findall("\\\\(?:input|include)\\{([^\\}]+)\\}", content)
    for f in inputs:
        if import_base is not None:
            f = os.path.join(import_base, f)
        
        if not os.path.exists(f):
            #probably include add .tex extension
            f += ".tex"
        keys |= set(get_keys(f))

    # find subimports and recursively parse them
    subimports = re.findall("\\\\subimport\*?\\{(.*)\\}\\{(.*)\\}", content)
    for f in subimports:
        filepath = os.path.join(f[0], f[1])
        keys |= set(get_keys(filepath, import_base=f[0]))

    keys = sorted(list([k.strip() for k in keys]))
    return keys


def keys_have_changed(keys):
    new_keys = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    old_keys = ""
    try:
        old_keys = open("main.bib.keys.sha").read().strip()
    except:
        pass
    try:
        open("main.bib.keys.sha", "w").write(new_keys)
    except:
        pass
    return (new_keys != old_keys)


def bib_has_changed(bib):
    new_bib = hashlib.sha256(bib.strip().encode("utf-8")).hexdigest()
    old_bib = ""
    try:
        old_bib = open("main.bib.sha").read().strip()
    except:
        pass
    save_bib_hash()
    return (new_bib != old_bib)


def entry_by_key(key):
    for entry in bib_database.entries:
        if entry["ID"] == key:
            return entry
    return None


def entry_to_bibtex(entry):
    newdb = bibtexparser.bibdatabase.BibDatabase()
    newdb.entries = [ entry ]
    return bibtexparser.dumps(newdb)


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


def update_local_bib(key, new_entry):
    for (idx, entry) in enumerate(bib_database.entries):
        if entry["ID"] == key:
            bib_database.entries[idx] = new_entry
            break


def update_remote_bib(key, new_entry):
    response = requests.put(server + "entry/%s" % key, json = {"entry": new_entry, "token": token})
    if "success" in response.json() and not response.json()["success"]:
        show_error(response.json())

def add_remote_bib(key, entry):
    response = requests.post(server + "entry/%s" % key, json = {"entry": entry, "token": token})
    if "success" in response.json() and not response.json()["success"]:
        show_error(response.json())

def remove_remote_bib(key):
    response = requests.delete(server + "entry/%s%s" % (key, "/%s" % token if token else ""))
    if "success" in response.json() and not response.json()["success"]:
        show_error(response.json())


def remove_local_bib(key):
    for (idx, entry) in enumerate(bib_database.entries):
        if entry["ID"] == key:
            del bib_database.entries[idx]
            save_bib()


def save_bib_hash():
    try:
        bib = open("main.bib").read()
        open("main.bib.sha", "w").write(hashlib.sha256(bib.strip().encode("utf-8")).hexdigest())
    except:
        pass


def save_bib():
    with open('main.bib', 'w') as bibtex_file:
        bibtexparser.dump(bib_database, bibtex_file)
    save_bib_hash()


def show_error(obj):
    if "reason" in obj:
        if obj["reason"] == "access_denied":
            print("\u001b[31m[!] Access denied!\u001b[0m Your token is not valid for this operation. Verify whether the file '%s' contains a valid token." % args.token_file)
        elif obj["reason"] == "policy":
            for entry in obj["entries"]:
                print("\u001b[31m[!] Server policy rejected entry %s\u001b[0m. Reason: %s" % (entry["ID"], entry["reason"]))
        else:
            print("\u001b[31m[!] Unhandled error occurred!\u001b[0m Reason (%s) %s" % (obj["reason"], obj["message"] if "message" in obj else ""))
    else:
        print("\u001b[31m[!] Unknown error occurred!\u001b[0m")
    sys.exit(1)

action = args.action

parser = BibTexParser(common_strings=True)
parser.ignore_nonstandard_types = False
parser.homogenize_fields = True

if not os.path.exists("main.bib") or os.stat("main.bib").st_size == 0:
    bib_database = bibtexparser.loads("\n")
else:
    try:
        with open('main.bib') as bibtex_file:
            bib_database = bibtexparser.load(bibtex_file, parser)
            #print(bib_database.entries)
    except Exception as e:
        print("Malformed bibliography file!\n")
        print(e)
        sys.exit(1)

response = requests.get(server + "version")
try:
    version_info = response.json()
except:
    print("\u001b[31m[!] Could not get version info from server.\u001b[0m Is the server URL \"%s\" correct?" % server)
    sys.exit(1)

if version_info["version"] > version:
    print("[!] New version available, updating...")
    script = requests.get(server + version_info["url"])
    with open(sys.argv[0], "w") as sc:
        sc.write(script.text)
    print("Restarting...")
    os.execl(sys.executable, *([sys.executable]+sys.argv))


if action == "search":
    if len(args.query) < 3:
        print("Usage: %s search --query <query>" % sys.argv[0])
        sys.exit(1)
    response = requests.get(server + "search/" + args.query + ("/%s" % token if token else ""))
    print(response.text)

elif action == "sync":
    response = requests.get(server + "sync")
    print(response.text)

elif action == "get":
    keys = get_keys(args.tex)
    fetch = keys_have_changed(keys)
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
        response = requests.post(server + "update", json = {"entries": bib_database.entries, "token": token})
        result = response.json()
        if not result["success"]:
            if result["reason"] == "duplicate":
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
        response = requests.post(server + "get_json", json = {"entries": keys, "token": token})
        bib = response.json()
        if "success" in bib and not bib["success"]:
            show_error(bib)
        else:
            # merge local and remote database
            for entry in bib:
                if entry and "ID" in entry and not entry_by_key(entry["ID"]):
                    bib_database.entries.append(entry)
            save_bib()

            # suggest keys for unresolved keys
            for key in keys:
                if not entry_by_key(key):
                    response = requests.get(server + "suggest/" + key + ("/%s" % (token if token else "")))
                    suggest = response.json()
                    if "success" in suggest and not suggest["success"]:
                        show_error(suggest)
                    else:
                        print("Key '%s' not found%s %s" % (key, ", did you mean any of these?" if len(suggest["entries"]) > 0 else "", ", ".join(["'%s'" % e[1]["ID"] for e in suggest["entries"]])))

else:
    print("Unknown action '%s'" % action)
