ARG WORKSPACE_IMAGE=agent-wow-workspace:local
FROM ${WORKSPACE_IMAGE}

ARG CODEX_VERSION=0.169.0
RUN npm install -g @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force

COPY --from=duel-workspace --chown=node:node / /workspace/
USER node
RUN printf '/agent-wow/\n' >> .git/info/exclude \
    && mkdir -p builds feedback
USER root

COPY --chmod=755 client.py /usr/local/bin/local-client
COPY --chmod=755 start-client.sh /usr/local/bin/start-local-client
CMD ["/usr/local/bin/start-local-client"]
