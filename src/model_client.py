"""
LogiDoc-RAG model client: every call to the local Ollama models.

The ollama library's ready-made functions (ollama.embeddings, ollama.chat)
share one client that waits without limit, so a stopped or stuck Ollama
would hold a request, and an upload's ingestion lock, forever. The calls
here go through clients with time limits from config.py:

    connecting           OLLAMA_CONNECT_TIMEOUT   (LOGIDOC_OLLAMA_CONNECT_TIMEOUT)
    one embedding        EMBED_TIMEOUT            (LOGIDOC_EMBED_TIMEOUT)
    one chat answer      LLM_TIMEOUT              (LOGIDOC_LLM_TIMEOUT)

Every failure becomes a ModelServiceError whose message names no host,
address or library, so it can be shown to a user; the original error stays
attached (__cause__) and is logged here for the server log:

    ModelUnavailable     not reachable, connection lost, or the service failed (HTTP 5xx)
    ModelTimeout         no answer within the time limit
    ModelMisconfigured   the model is not installed (HTTP 404) or access was refused (401, 403)
    ModelBusy            the service is overloaded (HTTP 429, 503)
    ModelRejected        the service refused this request (other 4xx): the text, not the service

The first three have service_down set: the failure is about the service, so
every other document would fail the same way. Ingestion then stops its batch
and keeps the files for a retry (ingestion_service). ModelBusy is retried
there with a pause; ModelRejected is not retried.

Nothing is retried here.
"""

import logging
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import ollama

import config


log = logging.getLogger("logidoc.models")

EMBEDDING = "embedding"
LANGUAGE_MODEL = "language model"

# One client per kind of call, created on first use (the time limits are read
# then); httpx clients are safe to share between threads
CLIENTS = {}
CLIENTS_LOCK = threading.Lock()


class ModelServiceError(Exception):
    """
    A call to the local model service failed. The message is safe to show.
    """

    # The failure is about the service, not the document
    service_down = False


class ModelUnavailable(ModelServiceError, ConnectionError):
    """
    The model service could not be reached, dropped the connection or
    failed (HTTP 5xx). (Still a ConnectionError, which is what the ollama
    library raised for a refused connection.)
    """

    service_down = True


class ModelTimeout(ModelServiceError):
    """
    The model service did not answer within its time limit.
    """

    service_down = True


class ModelMisconfigured(ModelServiceError):
    """
    The model is not installed on the service, or the service refused
    access: an operator has to act before any call can work.
    """

    service_down = True


class ModelBusy(ModelServiceError):
    """
    The model service is overloaded; worth trying again after a pause.
    """


class ModelRejected(ModelServiceError):
    """
    The model service refused this request (for example the text it was
    given); other requests may still work.
    """


def time_limit(kind):

    return config.EMBED_TIMEOUT if kind == EMBEDDING else config.LLM_TIMEOUT


def client(kind):
    """
    The shared client for embeddings or for chat answers.
    """

    with CLIENTS_LOCK:

        if kind not in CLIENTS:
            CLIENTS[kind] = ollama.Client(
                timeout=httpx.Timeout(time_limit(kind), connect=config.OLLAMA_CONNECT_TIMEOUT)
            )

        return CLIENTS[kind]


def answered_with_error(kind, model, status):
    """
    The ModelServiceError for an error answer from the service
    (ollama.ResponseError), by its HTTP status.
    """

    if status == 404:
        return ModelMisconfigured(f"The {kind} model ({model}) is not installed on the model service.")

    if status in (401, 403):
        return ModelMisconfigured(f"The {kind} service refused access (HTTP {status}); check its configuration.")

    if status in (429, 503):
        return ModelBusy(f"The {kind} service is busy (HTTP {status}); try again shortly.")

    if 400 <= status < 500:
        return ModelRejected(f"The {kind} service rejected the request (HTTP {status}).")

    return ModelUnavailable(f"The {kind} service is unavailable (it failed with HTTP {status}).")


def call(kind, model, request):
    """
    Runs request(client) once and turns every failure into a
    ModelServiceError.
    """

    unavailable = f"The {kind} service is unavailable (it could not be reached)."

    try:
        return request(client(kind))
    except httpx.ConnectTimeout as error:
        failure, cause = ModelUnavailable(unavailable), error
    except httpx.TimeoutException as error:
        failure, cause = ModelTimeout(f"The {kind} service did not answer within {time_limit(kind):g} s (timed out)."), error
    except (ConnectionError, httpx.TransportError) as error:
        # ollama turns a refused connection into ConnectionError; a dropped
        # one arrives as an httpx transport error
        failure, cause = ModelUnavailable(unavailable), error
    except ollama.ResponseError as error:
        failure, cause = answered_with_error(kind, model, error.status_code), error

    # The service's own words (never the request, so never document text)
    log.warning("The %s call failed: %s: %s", kind, type(cause).__name__, cause)

    raise failure from cause


def embeddings(model, prompt):
    """
    ollama.embeddings with a time limit: the same response.
    """

    return call(EMBEDDING, model, lambda service: service.embeddings(model=model, prompt=prompt))


def chat(model, messages):
    """
    ollama.chat with a time limit: the same response.
    """

    return call(LANGUAGE_MODEL, model, lambda service: service.chat(model=model, messages=messages))


def installed_models(limit=2.0):
    """
    The names of the models the service holds, from its model list (nothing
    is loaded, embedded or generated), or None when it does not answer within
    limit seconds or answers with an error. Used by the readiness check.
    """

    try:
        with ollama.Client(timeout=httpx.Timeout(limit)) as service:
            listing = service.list()
    except (ConnectionError, httpx.HTTPError, ollama.ResponseError) as error:
        log.warning("The model list could not be read: %s: %s", type(error).__name__, error)
        return None

    return {item.model for item in listing.models if item.model}


def has_model(installed, name):
    """
    Whether name is in installed. Ollama lists names with a tag
    (nomic-embed-text:latest); config may leave the tag out.
    """

    return name in installed or (":" not in name and f"{name}:latest" in installed)
