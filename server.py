import bibtexparser
from flask import Flask, jsonify, request
import Levenshtein
import git
import json
import sys
import importlib
import hmac
import hashlib
import os
import re
import threading

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

VERSION = 18

app = Flask(__name__)
tokens = True
no_commit = False
token_db = {}
tokens_loaded = False
db_lock = threading.RLock()

repo = None
repo_path = None
repo_name = None
policy = None
bib_database = None

# Optional secret for /v1/webhook (GitLab "Secret token" or GitHub "Secret"). If unset, the webhook is unauthenticated.
webhook_secret = os.environ.get("BIBTOOL_WEBHOOK_SECRET", "")

MAX_FIELD_LENGTH = 100000
ID_RE = re.compile(r'^[^\s,{}()"#%=\\]+$')
ENTRYTYPE_RE = re.compile(r'^[A-Za-z]+$')
FIELD_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_\-:.+]*$')


def check_token(token, operation):
    if not tokens:
        return (True, None)

    if len(token_db) == 0:
        return (False, {"success": False, "reason": "server_problem", "message": "The token database on the server seems to be corrupted, please inform your BibTool administrator."})

    if not isinstance(token, str) or not token in token_db:
        return (False, {"success": False, "reason": "access_denied", "message": "Invalid token. Check your 'token' file."})
    perms = token_db[token]
    if not isinstance(perms, dict) or not perms.get(operation, False):
        return (False, {"success": False, "reason": "access_denied", "message": "Your token does not grant %s access." % operation})
    return (True, None)


def _braces_balanced(value, honor_escapes):
    depth = 0
    i = 0
    while i < len(value):
        c = value[i]
        if honor_escapes and c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def validate_entry(entry):
    """Returns None if the entry is well-formed, otherwise an error message."""
    if not isinstance(entry, dict):
        return "Entry must be an object."
    if not isinstance(entry.get("ID"), str) or not ID_RE.match(entry["ID"]):
        return "Invalid citation key %r." % (entry.get("ID"),)
    if not isinstance(entry.get("ENTRYTYPE", "misc"), str) or not ENTRYTYPE_RE.match(entry.get("ENTRYTYPE", "misc")):
        return "Invalid entry type for %s." % entry["ID"]
    for field, value in entry.items():
        if field in ("ID", "ENTRYTYPE"):
            continue
        if not isinstance(field, str) or not FIELD_RE.match(field):
            return "Invalid field name %r in %s." % (field, entry["ID"])
        if not isinstance(value, str):
            return "Field %s in %s must be a string." % (field, entry["ID"])
        if len(value) > MAX_FIELD_LENGTH:
            return "Field %s in %s is too long." % (field, entry["ID"])
        # BibTeX counts every brace, bibtexparser skips escaped ones -> must be balanced for both
        if not _braces_balanced(value, False) or not _braces_balanced(value, True):
            return "Field %s in %s has unbalanced braces." % (field, entry["ID"])
    return None


def invalid_response(message):
    return jsonify({"success": False, "reason": "invalid_entry", "message": message})


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
    bib_dump(bib_database, repo_path + "/" + repo_name)
    if repo and not no_commit:
        msg = commit_message if commit_message else "update"
        if tokens:
            msg += " (Token %s)" % (token if token else "none")
        msg = "[BibTool] %s" % msg
        repo.index.add(repo_path + "/" + repo_name)
        repo.index.commit(msg)
        try:
            origin = repo.remotes.origin
        except Exception:
            return
        # integrate remote changes first, otherwise the push is rejected forever
        try:
            origin.pull(rebase=True)
        except Exception:
            print("Warning: could not pull from repository before pushing")
            try:
                repo.git.rebase("--abort")
            except Exception:
                pass
            return
        try:
            origin.push()
        except Exception:
            print("Warning: could not push to repository")
        # the pull might have brought in changes made outside of BibTool
        load_data()

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
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.py")).read(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/v1/requirements.txt")
def get_reqtxt():
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")).read(), 200, {"Content-Type": "text/plain; charset=utf-8"}


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
    entry = entry_by_key(key)
    if not entry:
        return "Entry not found", 404
    return entry_to_bibtex(entry)


@app.route("/v1/get", methods=["POST"])
def get_bibfile():
    if not request.json or not "entries" in request.json or not "token" in request.json:
        return "Invalid request"
    ok, reason = check_token(request.json["token"], "read")
    if not ok:
        return reason["message"]

    bib = ""
    for key in request.json["entries"]:
        entry = entry_by_key(key)
        if entry:
            bib += entry_to_bibtex(entry) + "\n"

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

    entry = request.json["entry"]
    if isinstance(entry, dict) and "ID" not in entry:
        entry["ID"] = key
    error = validate_entry(entry)
    if error:
        return invalid_response(error)

    with db_lock:
        existing = entry_by_key(entry["ID"])
        if existing:
            return jsonify({"success": False, "reason": "exists", "entry": existing})

        if policy and "force" not in request.json:
            accept, reason = policy.check(entry, bib_database.entries)
            if not accept:
                entry["reason"] = reason
                return jsonify({"success": False, "reason": "policy", "entries": [entry]})

        bib_database.entries.append(entry)
        save_bib("Added %s" % entry["ID"], request.json["token"])
    return jsonify({"success": True})


@app.route("/v1/entry/<string:key>", methods=["PUT"])
def replace_entry(key):
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

    entry = request.json["entry"]
    if isinstance(entry, dict) and "ID" not in entry:
        entry["ID"] = key
    error = validate_entry(entry)
    if error:
        return invalid_response(error)

    with db_lock:
        if entry["ID"] != key and entry_by_key(entry["ID"]):
            return jsonify({"success": False, "reason": "exists", "entry": entry_by_key(entry["ID"])})

        if policy and "force" not in request.json:
            accept, reason = policy.check(entry, bib_database.entries)
            if not accept:
                entry["reason"] = reason
                return jsonify({"success": False, "reason": "policy", "entries": [entry]})

        for (idx, existing) in enumerate(bib_database.entries):
            if existing["ID"] == key:
                bib_database.entries[idx] = entry
                save_bib("Changed %s" % key, request.json["token"])
                return jsonify({"success": True})

    return jsonify({"success": False, "reason": "not_found"})


@app.route("/v1/entry/<string:key>", defaults={"token": None}, methods=["DELETE"])
@app.route("/v1/entry/<string:key>/<string:token>", methods=["DELETE"])
def remove_entry(key, token):
    ok, reason = check_token(token, "delete")
    if not ok:
        return jsonify(reason)

    with db_lock:
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

    if not isinstance(request.json["entries"], list):
        return invalid_response("Entries must be a list.")
    for entry in request.json["entries"]:
        error = validate_entry(entry)
        if error:
            return invalid_response(error)

    dups = []
    changes = False
    changelog = []
    rejects = []
    with db_lock:
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


def load_data():
    global bib_database, token_db, tokens, tokens_loaded

    with db_lock:
        bib_database = bib_load(repo_path + "/" + repo_name)

        try:
            with open(repo_path + "/tokens.json") as tdb:
                token_db = json.load(tdb)
            if not isinstance(token_db, dict):
                raise ValueError("tokens.json must contain an object")
            tokens = True
        except FileNotFoundError:
            if not tokens_loaded:
                print("No tokens.json, disable token checks")
                tokens = False
            else:
                # never silently turn off authentication at runtime
                print("Error: tokens.json disappeared, keeping previous token configuration! Restart the server to disable token checks.")
        except Exception:
            print("Error: error in the tokens.json, could not load it!")
            token_db = {}
            tokens = True
        tokens_loaded = True


def sync():
    global repo

    with db_lock:
        repo = git.Repo(repo_path)
        try:
            origin = repo.remotes.origin
            origin.pull()
        except Exception:
            print("Warning: could not pull from repository")

        load_data()

    return "Synced!"


@app.route("/v1/sync", defaults={"token": None}, methods=["GET"])
@app.route("/v1/sync/<string:token>", methods=["GET"])
def sync_route(token):
    ok, reason = check_token(token, "write")
    if not ok:
        return reason["message"], 403
    return sync()


def webhook_authorized():
    if not webhook_secret:
        return True
    # GitLab / Gogs / Gitea: plain secret token
    for header in ("X-Gitlab-Token", "X-Gogs-Token", "X-Gitea-Token"):
        value = request.headers.get(header)
        if value is not None and hmac.compare_digest(value.encode("utf-8"), webhook_secret.encode("utf-8")):
            return True
    # GitHub / Gitea / Gogs: HMAC-SHA256 signature of the body
    expected = hmac.new(webhook_secret.encode("utf-8"), request.get_data(), hashlib.sha256).hexdigest()
    for header in ("X-Hub-Signature-256", "X-Gitea-Signature", "X-Gogs-Signature"):
        value = request.headers.get(header)
        if value is not None:
            value = value.split("=", 1)[1] if value.startswith("sha256=") else value
            if hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
                return True
    return False


@app.route("/v1/webhook", methods=["POST"])
def webhook():
    if not webhook_authorized():
        return jsonify({"success": False, "reason": "access_denied", "message": "Invalid or missing webhook secret."}), 403
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


def init(path, name, policy_name=None):
    global repo_path, repo_name, policy

    repo_path = path
    repo_name = name
    policy = None
    if policy_name:
        print("Import policy %s" % policy_name)
        try:
            policy = importlib.import_module(policy_name)
        except Exception as e:
            print("Error: could not import policy %s: %s" % (policy_name, e))
            sys.exit(1)
    if not webhook_secret:
        print("Warning: BIBTOOL_WEBHOOK_SECRET is not set, the webhook accepts unauthenticated requests")
    sync()


# allow running under a WSGI server (e.g. gunicorn server:app)
if __name__ != "__main__" and os.environ.get("BIBTOOL_REPO"):
    init(os.environ["BIBTOOL_REPO"], os.environ.get("BIBTOOL_BIB", "main.bib"), os.environ.get("BIBTOOL_POLICY"))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: %s <repo path> <bib filename> [<policy>]" % sys.argv[0])
        sys.exit(1)
    init(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)

    app.run(debug=False, host='0.0.0.0')
