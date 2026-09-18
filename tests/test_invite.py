from chat_parser.ingest.collector import invite_hash


def test_private_invite_forms():
    h = "Wfx5U9BAtSRiODVi"
    for ref in (
        f"https://t.me/+{h}",
        f"t.me/+{h}",
        f"https://t.me/joinchat/{h}",
        f"+{h}",
    ):
        assert invite_hash(ref) == h, ref


def test_public_refs_are_not_invites():
    for ref in ("@optika_chat", "https://t.me/optika_chat", "-1001234567890"):
        assert invite_hash(ref) is None, ref
