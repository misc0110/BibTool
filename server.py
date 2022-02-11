import bibtexparser
from bibtexparser.bparser import BibTexParser
from flask import Flask, jsonify, request
import Levenshtein
import git
import json
import sys
import importlib

VERSION = 15

app = Flask(__name__)
tokens = True
no_commit = False

token_db = {
  "test": {"search": True, "read": True, "write": True, "delete": True}
}

def check_token(token, operation):
    if not tokens:
        return (True, None)

    if len(token_db) == 0:
        return (False, {"success": False, "reason": "server_problem", "message": "The token database on the server seems to be corrupted, please inform your BibTool administrator."})

    if not token in token_db:
        return (False, {"success": False, "reason": "access_denied", "message": "Invalid token. Check your 'token' file."})
    if not operation in token_db[token]:
        return (False, {"success": False, "reason": "access_denied", "message": "Your token does not grant %s access." % operation})
    ok = token_db[token][operation]
    if not ok:
        return (False, {"success": False, "reason": "access_denied", "message": "Your token does not grant %s access." % operation})
    else:
        return (True, None)

def entry_to_bibtex(entry):
    newdb = bibtexparser.bibdatabase.BibDatabase()
    newdb.entries = [ entry ]
    return bibtexparser.dumps(newdb)


def get_duplicates(entry):
    dups = []
    for e in bib_database.entries:
        dist = 0
        fields = set(e.keys())
        fields.update(entry.keys())
        length = 0
        exact = e["ID"] == entry["ID"]
        for field in fields:
            if field in e and field in entry:
                dist += Levenshtein.distance(e[field], entry[field])
                length += max(len(e[field]), len(entry[field]))
        if (exact and sorted(e.keys()) != sorted(entry.keys())) or ((dist < max(5, length * 0.1) or exact) and dist > 0):
            dups.append((dist, entry["ID"], e))
    return dups


def entry_by_key(key):
    for entry in bib_database.entries:
        if entry["ID"] == key:
            return entry
    return None


def save_bib(commit_message = None, token = None):
    with open(repo_path + "/" + repo_name, "w") as bibtex_file:
        bibtexparser.dump(bib_database, bibtex_file)
    if repo and not no_commit:
        msg = commit_message if commit_message else "update"
        if tokens:
            msg += " (Token %s)" % (token if token else "none")
        msg = "[BibTool] %s" % msg
        repo.index.add(repo_path + "/" + repo_name)
        repo.index.commit(msg)
        try:
            repo.remotes.origin.push()
        except:
            print("Warning: could not push to repository")

def entry_is_same(e1, e2):
    if set(e1.keys()) != set(e2.keys()):
        return False
    for f in e1.keys():
        if e1[f] != e2[f]:
            return False
    return True


@app.route("/")
def index():
    return "BibTool v1<br/>\n<a href=\"v1/client.py\">Download client</a><br/>\n<a href=\"v1/requirements.txt\">Download requirements.txt</a>"


@app.route("/v1/client.py")
def get_client():
    return open("client.py").read()


@app.route("/v1/requirements.txt")
def get_reqtxt():
    return open("requirements.txt").read()


@app.route("/v1/entry/<string:key>", defaults={"token": None}, methods=["GET"])
@app.route("/v1/entry/<string:key>/<string:token>", methods=["GET"])
def get_entry(key, token):
    ok, reason = check_token(token, "read")
    if not ok:
        return jsonify(reason)
    return jsonify({"success": True, "entry": entry_by_key(key)})


@app.route("/v1/bibentry/<string:key>", defaults={"token": None}, methods=["GET"])
@app.route("/v1/bibentry/<string:key>/<string:token>", methods=["GET"])
def get_bibentry(key, token):
    ok, reason = check_token(token, "read")
    if not ok:
        return reason["message"]
    return entry_to_bibtex(entry_by_key(key))


@app.route("/v1/get", methods=["POST"])
def get_bibfile():
    if not request.json or not "entries" in request.json or not "token" in request.json:
        return "Invalid request"
    ok, reason = check_token(request.json["token"], "read")
    if not ok:
        return reason["message"]

    bib = ""
    for entry in request.json["entries"]:
        bib += entry_to_bibtex(entry_by_key(entry)) + "\n"

    return bib


@app.route("/v1/get_json", methods=["POST"])
def get_bibfile_as_json():
    if not request.json or not "entries" in request.json or not "token" in request.json:
        return jsonify({"success": False, "reason": "invalid_request", "message": "Invalid request"})
    ok, reason = check_token(request.json["token"], "read")
    if not ok:
        return jsonify(reason)

    bib = []
    for entry in request.json["entries"]:
        bib.append(entry_by_key(entry))

    return jsonify(bib)


@app.route("/v1/suggest/<string:key>", defaults={"token": None}, methods=["GET"])
@app.route("/v1/suggest/<string:key>/<string:token>", methods=["GET"])
def suggest_entry(key, token):
    ok, reason = check_token(token, "search")
    if not ok:
        return jsonify(reason)

    entry = entry_by_key(key)
    if not entry:
        entries = []
        for entry in bib_database.entries:
            dist = Levenshtein.distance(entry["ID"].lower(), key.lower())
            if key.lower() in entry["ID"].lower() or dist == 0:
                entries.append((1, entry))
                continue
            if dist < 5:
                entries.append((1-dist/100.0, entry))
                continue
            common_prefix = 0
            for i in range(min(len(entry["ID"]), len(key))):
                if entry["ID"].lower()[i] != key.lower()[i]:
                    break
                common_prefix += 1
            if common_prefix >= 6:
                entries.append((common_prefix/float(max(len(entry["ID"]), len(key))), entry))
    else:
        entries = [ (1, entry) ]

    top = sorted(entries, key=lambda x: x[0], reverse=True)
    return jsonify({"success": True, "entries": top[:5]})


@app.route("/v1/search/<string:query>", defaults={"token": None}, methods=["GET"])
@app.route("/v1/search/<string:query>/<string:token>", methods=["GET"])
def search_entry(query, token):
    ok, reason = check_token(token, "search")
    if not ok:
        return reason["message"]

    query_parts = query.split(" ")
    for q in query_parts:
        if len(q) < 3:
            return "Each query must be at least 3 characters!"
    entries = []
    for entry in bib_database.entries:
        found_part = [False for q in query_parts]
        for field in entry:
            for (idx, q) in enumerate(query_parts):
                if field.lower() != "entrytype" and q.lower() in entry[field].lower():
                    found_part[idx] = True
        was_found = True
        for q in found_part:
            was_found &= q
        if was_found:
            entries.append((entry_to_bibtex(entry)))
    return "\n".join(list(set(entries)))


@app.route("/v1/entry/<string:key>", methods=["POST"])
def add_entry(key):
    if not request.json or not "entry" in request.json or not "token" in request.json:
        return jsonify({"success": False, "reason": "missing_entry"})
    ok, reason = check_token(request.json["token"], "write")
    if not ok:
        return jsonify(reason)
    # check if the client is forcing the server policy
    if "force" in request.json:
        ok, reason = check_token(request.json["token"], "force")
        if not ok:
            return jsonify(reason)

    if "ID" not in request.json["entry"]:
        request.json["entry"]["ID"] = key

    existing = entry_by_key(request.json["entry"]["ID"])
    if existing:
        return jsonify({"success": False, "reason": "exists", "entry": existing})

    if policy and "force" not in request.json:
        accept, reason = policy.check(request.json["entry"], bib_database.entries)
        if not accept:
            entry = request.json["entry"]
            entry["reason"] = reason
            return jsonify({"success": False, "reason": "policy", "entries": [entry]})

    bib_database.entries.append(request.json["entry"])
    save_bib("Added %s" % request.json["entry"]["ID"], request.json["token"])
    return jsonify({"success": True})


@app.route("/v1/entry/<string:key>", methods=["PUT"])
def replace_entry(key):
    if not request.json or not "entry" in request.json or not "token" in request.json:
        return jsonify({"success": False, "reason": "missing_entry"})
    ok, reason = check_token(request.json["token"], "write")
    if not ok:
        return jsonify(reason)

    for (idx, entry) in enumerate(bib_database.entries):
        if entry["ID"] == key:
            bib_database.entries[idx] = request.json["entry"]
            save_bib("Changed %s" % key, request.json["token"])
            return jsonify({"success": True})

    return jsonify({"success": False, "reason": "not_found"})


@app.route("/v1/entry/<string:key>", defaults={"token": None}, methods=["DELETE"])
@app.route("/v1/entry/<string:key>/<string:token>", methods=["DELETE"])
def remove_entry(key, token):
    ok, reason = check_token(token, "delete")
    if not ok:
        return jsonify(reason)

    for (idx, entry) in enumerate(bib_database.entries):
        if entry["ID"] == key:
            del bib_database.entries[idx]
            save_bib("Deleted %s" % key, token)
            return jsonify({"success": True})

    return jsonify({"success": False, "reason": "not_found"})


@app.route("/v1/update", methods=["POST"])
def add_entries():
    if not request.json or not "entries" in request.json or not "token" in request.json:
        return jsonify({"success": False, "reason": "missing_entry"})
    ok, reason = check_token(request.json["token"], "write")
    if not ok:
        return jsonify(reason)
    # check if the client is forcing the server policy
    if "force" in request.json:
        ok, reason = check_token(request.json["token"], "force")
        if not ok:
            return jsonify(reason)

    dups = []
    changes = False
    changelog = []
    rejects = []
    for entry in request.json["entries"]:
        existing = entry_by_key(entry["ID"])
        if existing and entry_is_same(existing, entry):
            continue

        dup = get_duplicates(entry)
        if len(dup) == 0:
            # new entry, add
            if not entry_by_key(entry["ID"]):
                if policy and "force" not in request.json:
                    accept, reason = policy.check(entry, bib_database.entries)
                    if not accept:
                        entry["reason"] = reason
                        rejects.append(entry)
                        print("Rejecting entry %s" % entry["ID"])
                        continue
                bib_database.entries.append(entry)
                changelog.append("Added %s" % entry["ID"])
                changes = True
        else:
            dups += dup

    if changes:
        save_bib("\n".join(changelog), request.json["token"])

    if len(rejects) > 0:
        return jsonify({"success": False, "reason": "policy", "entries": rejects})

    if len(dups) > 0:
        return jsonify({"success": False, "reason": "duplicate", "entries": dups})

    return jsonify({"success": True})


@app.route("/v1/sync", methods=["GET"])
def sync():
    global repo, bib_database, token_db, tokens

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = True

    repo = git.Repo(repo_path)
    try:
        origin = repo.remotes.origin
        origin.pull()
    except:
        print("Warning: could not pull from repository")

    with open(repo_path + "/" + repo_name) as bibtex_file:
        bib_database = bibtexparser.load(bibtex_file, parser)

    # uncomment for debug purposes
    #for e in bib_database.entries:
        #if policy:
            #accept, reason = policy.check(e, bib_database.entries)
            #if not accept:
                #print("Reject %s: %s" % (e["ID"], reason))

    try:
        tdb = open(repo_path + "/tokens.json")
        token_db = json.load(tdb)
    except IOError:
        print("No tokens.json, disable token checks")
        tokens = False
    except:
        print("Error: error in the tokens.json, could not load it!")
        token_db = {}

    return "Synced!"


@app.route("/v1/webhook", methods=["POST"])
def webhook():
    if not request.json or not "commits" in request.json:
        return jsonify({"success": False, "reason": "missing_entry"})

    was_internal = True
    for commit in request.json["commits"]:
        if "title" in commit and "[BibTool]" not in commit["title"]:
            was_internal = False
            break
        if "message" in commit and "[BibTool]" not in commit["message"]:
            was_internal = False
            break

    if not was_internal:
        return sync()
    else:
        return "OK"


@app.route("/v1/version", methods=["GET"])
def version():
    return jsonify({"version": VERSION, "url": "client.py"})


if __name__ == "__main__":
    global repo_path, repo_name, policy

    if len(sys.argv) < 3:
        print("Usage: %s <repo path> <bib filename> [<policy>]" % sys.argv[0])
        sys.exit(1)
    repo_path = sys.argv[1]
    repo_name = sys.argv[2]
    if len(sys.argv) > 3:
        try:
            print("Import policy %s" % sys.argv[3])
            policy = importlib.import_module(sys.argv[3])
        except:
            policy = None
    else:
        policy = None

    sync()

    app.run(debug=False, host='0.0.0.0')
