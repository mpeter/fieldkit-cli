### Make Tach test selection deterministic (#73)

Tach impact selection now uses non-overlapping source roots for deterministic test selection. PR checks also run checkout hook and script tests independently of Tach, with automatic discovery of new tool tests.

Hook import boundaries remain enforced without overlapping Tach roots. Tool test selection follows helper package re-exports, and CI reports checkout-tool test evidence with its own scope.

Tool tests are retained when they consume tool-backed fixtures or pytest hooks defined in conftest.py or registered local pytest plugins, including renamed fixtures, helper aliases, autouse fixtures, and collection-time parameter generation.
