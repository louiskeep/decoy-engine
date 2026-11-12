"""Seeded violation for the support no-network sentry: a support module that could upload."""

import urllib.request
from http import client


def send(bundle: bytes) -> object:
    import socket

    from google import cloud

    return (urllib.request.Request, client, socket, cloud, bundle)
