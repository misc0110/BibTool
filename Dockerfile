FROM ubuntu:latest
MAINTAINER Michael Schwarz

COPY server.py client.py requirements.txt /opt/
WORKDIR /opt

RUN apt-get update && \
    apt-get install -y python3 python3-pip git && \
    pip3 install flask python-Levenshtein bibtexparser GitPython && \
    git config --global user.email "bib@to.ol" && \
    git config --global user.name "BibTool"

ENTRYPOINT ["python3", "server.py", "/data/", "main.bib", "policy"]

EXPOSE 5000

