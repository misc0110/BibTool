FROM ubuntu:latest
MAINTAINER Michael Schwarz

COPY server.py client.py requirements.txt /opt/
WORKDIR /opt

RUN apt-get update && \
    apt-get install -y python3 python3-pip git && \
    pip3 install flask python-Levenshtein bibtexparser GitPython

ENTRYPOINT ["python3", "server.py", "/data/", "main.bib"]

EXPOSE 5000

