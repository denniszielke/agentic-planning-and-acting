FROM python:3.13-slim
WORKDIR /app
COPY ./src/travel_mcp_server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY ./src /app/src
ENV TRAVEL_MCP_HOST=0.0.0.0
ENV TRAVEL_MCP_PORT=8093
EXPOSE 8093
CMD ["python", "-m", "src.travel_mcp_server.server"]