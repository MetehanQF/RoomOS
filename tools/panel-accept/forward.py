"""Loopback-only TCP forwarder: 127.0.0.1:<port> -> acceptance Web container on its --internal network.

usage: forward.py <web-container-ip> <port>
"""
import asyncio
import sys

TARGET = (sys.argv[1], 8443)
PORT = int(sys.argv[2])


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_reader, client_writer):
    target_reader, target_writer = await asyncio.open_connection(*TARGET)
    await asyncio.gather(pipe(client_reader, target_writer), pipe(target_reader, client_writer), return_exceptions=True)


async def main():
    server = await asyncio.start_server(handle, '127.0.0.1', PORT)
    async with server:
        await server.serve_forever()


asyncio.run(main())
