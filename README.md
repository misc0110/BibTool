# BibTool

A tool to manage bibliography when collaboratively working on a LaTeX paper. 

# Setup

TL;DR:
* Create a Git repo containing `main.bib` and `tokens.json`, that can be pushed/pulled without credentials (e.g., using an access token)
* Add a push webhook to notify your server on changes to the repo
* Start the docker container with the server

## Client
The client is a simple Python 3 script that only requires three dependencies: `bibtexparser`, `requests`, and `argparse`. 
They can simply be installed using pip: `pip3 install -r requirements.txt`.

## Bibliography
The bibliography is stored in a Git-versioned repository to be able to track (and revert) changes. 
The repository also contains the access tokens required by the clients to interact with the server. 

### Bibliography File
The bibliography file has to be in a Git repository where the server can pull from and push to. 
The easiest way is to create an access token in GitHub, GitLab, or Gogs and use this access token to clone the repo over HTTPS (`git clone https://<token>@github.com/owner/repo.git`).
The bibliography file has to be named `main.bib`. 

### Authentication
Authentication is handled via tokens defined in `tokens.json`. The file has the following format:
```
{
    "token1": {
        "search": true,
        "read": true,
        "write": true,
        "delete": true,
        "force": true
    },
    "token2": {
        "search": true,
        "read": true,
        "write": false,
        "delete": false
    }
}
```
For each request to the server, the client has to provide a token. 
The server can then check whether the client is allowed to perform the requested action. 
The permissions are:
* `search`: Search for bibliography entries containing a certain string
* `read`: Get a bibliography entry based on an identifier
* `write`: Add or modify bibliography entries
* `delete`: Delete bibliography entries
* `force`: Allow bibliography entries writes to bypass server policy

## Server
The server runs inside a Docker container and works on a Git-versioned bibliography file outside the container. 
A webhook ensures that manual edits of the bibliography file and authentication-token file are propagated to the server. 

### Webhook
To allow manual changes to the bibliography file or the authentication tokens without having to restart the server, it is necessary to configure a webhook. 
The webhook has to send a notification to `<your bib server>/v1/webhook` on push events. There is no secret token required. 

### Policy
The tool allows defining a policy for bibliography entries. 
If enabled, a new entry can only be added if it passes the checks of the policy. 
The policy is written as a callback function in Python. 
It has accecss to the entry that should be added, as well as the entire database containing all entries. 
A simple policy is provided in `policy.py`: it rejects entries where the citation key has a length of 0, and accepts all other entries. 

### Run Server
* Build the Docker container: `docker build --tag bibtool .`
* Start the Docker container: `docker run -p 5000:5000 -v <path to bibliography folder>:/data bibtool`

By default, the server runs on port 5000. 

# Usage

The general workflow of using BibTool is the following. 
The client parses the TeX file to extract all citation keys, sends them to the server, and stores the corresponding bibliography entries in `main.bib`. 

## Getting a bibliography file
The client is simply invoked by running `python3 client.py get --server <your bib server>`. 
The name of the authentication token can either be stored in the file `token` in the same folder, or it can alternatively be provided using the `--token <tokenname>` parameter. 
The LaTeX file is assumed to be named `main.tex`. This can be changed with the `--tex <latex file>` parameter. 

If a key is not found, and the bibliography file was not modified locally, the server returns suggestions for similar keys. 
In case the bibliography entry was added locally, the entry is added to the bibliography repository. 
If there is a collision, i.e., the same key exists locally and remotely, the user has to decide how to handle the situation (abort, overwrite server entry with local entry, discard local entry and get server entry). 
Hence, adding or modifying bibliography entries is as simple as adding or modifying them and rerunning the client. 

## Searching for bibliography entries
The client also supports searching for entries: `python3 client.py search --query <search query> --server <your bib server>`. 
If multiple queries are provided, all of them have to match. 
Note that each query has to be at least 3 characters long. 

## Automatically Update
The client supports automated updates. If the version number of the client is lower than the one provided by the server, the client automatically fetches the new client from the server and restarts itself. 
