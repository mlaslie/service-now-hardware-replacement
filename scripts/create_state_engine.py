"""Creates the Agent Runtime instance that stores conversations and memory.

It runs no code: the agent runs on Cloud Run and uses this instance only for
Agent Runtime's managed Sessions and Memory Bank. Prints the ID to put in .env
as AGENT_ENGINE_ID.

    uv run python scripts/create_state_engine.py                 # project/region from .env
    uv run python scripts/create_state_engine.py --delete <ID>   # remove one (e.g. a test)
"""

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config  # noqa: E402  (reads .env)

DISPLAY_NAME = "hardware-replacement-state"

# What Memory Bank remembers about each person, across conversations. Delivery
# addresses are not here: the agent saves those verbatim itself (app/memory.py).
MEMORY_TOPICS = [
    {"managed_memory_topic": {"managed_topic_enum": "USER_PREFERENCES"}},
    {"custom_memory_topic": {
        "label": "delivery_and_contact",
        "description": "How the user prefers to be contacted (phone number, time windows) and any "
                       "standing delivery preferences.",
    }},
    {"custom_memory_topic": {
        "label": "hardware_history",
        "description": "Devices the user has reported problems with, the kind of problem, and ticket "
                       "numbers of past hardware requests.",
    }},
]


def _client():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import vertexai
        return vertexai.Client(project=config.PROJECT_ID, location=config.AGENT_ENGINE_LOCATION)


def create() -> str:
    engine = _client().agent_engines.create(config={
        "display_name": DISPLAY_NAME,
        "description": "Session and Memory Bank store for the Hardware Replacement agent on Cloud Run. Runs no code.",
        "context_spec": {"memory_bank_config": {
            "customization_configs": [{"memory_topics": MEMORY_TOPICS}],
            "similarity_search_config": {"embedding_model": (
                f"projects/{config.PROJECT_ID}/locations/{config.AGENT_ENGINE_LOCATION}"
                "/publishers/google/models/gemini-embedding-001")},
        }},
    })
    return engine.api_resource.name.rsplit("/", 1)[-1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--delete", metavar="ID", help="delete this instance instead")
    args = ap.parse_args()
    if not config.PROJECT_ID:
        sys.exit("Set GOOGLE_CLOUD_PROJECT in .env first (copy .env.example).")
    if args.delete:
        name = f"projects/{config.PROJECT_ID}/locations/{config.AGENT_ENGINE_LOCATION}/reasoningEngines/{args.delete}"
        _client().agent_engines.delete(name=name, force=True)
        print(f"Deleted {args.delete}")
        return
    engine_id = create()
    print(f"Created {DISPLAY_NAME} in {config.AGENT_ENGINE_LOCATION}.\nAdd to .env:\n\nAGENT_ENGINE_ID={engine_id}")


if __name__ == "__main__":
    main()
