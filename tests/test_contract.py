"""NodeContract conformance — the suite `tamtree plugin test` exists to run."""

from tamtree_plugin_sdk import Item
from tamtree_plugin_sdk.testing import FakeContext, NodeContract, NodeTestKit

from tamtree_shortvideo.nodes import NODE_NAME, ShortVideoSelfTestNode


def _context(**params: object) -> FakeContext:
    return NodeTestKit(ShortVideoSelfTestNode()).params(**params).context()


class TestSelfTestContract(NodeContract):
    def make_node(self) -> ShortVideoSelfTestNode:
        return ShortVideoSelfTestNode()

    def make_context(self) -> FakeContext:
        return _context(note="hello")


async def test_echoes_one_row_per_input_item() -> None:
    node = ShortVideoSelfTestNode()
    ctx = (
        NodeTestKit(node)
        .params(note="ping")
        .inputs(
            "main",
            [Item.model_validate({"json": {"a": 1}}), Item.model_validate({"json": {"a": 2}})],
        )
        .context()
    )

    output = await node.execute(ctx)

    assert len(output["main"]) == 2
    for item in output["main"]:
        assert item.json_["ok"] is True
        assert item.json_["node"] == NODE_NAME
        assert item.json_["note"] == "ping"
        assert item.json_["contracts_version"]


async def test_empty_input_still_answers() -> None:
    """A self test handed nothing must still say whether the plugin loaded."""
    node = ShortVideoSelfTestNode()

    output = await node.execute(_context(note=""))

    assert len(output["main"]) == 1
    assert output["main"][0].json_["ok"] is True
