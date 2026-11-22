FROM python:3.13-slim
LABEL maintainer="Michael Schwarz"

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    git config --global user.email "bib@to.ol" && \
    git config --global user.name "BibTool" && \
    git config --global --add safe.directory '*'

WORKDIR /opt
COPY requirements_server.txt /opt/
RUN pip install --no-cache-dir -r requirements_server.txt

COPY server.py client.py requirements.txt policy.py /opt/

ENTRYPOINT ["python3", "server.py", "/data/", "main.bib", "policy"]

EXPOSE 5000
