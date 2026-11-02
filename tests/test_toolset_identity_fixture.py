"""Collision, replay and request-isolation controls for a naming experiment."""

import itertools
import unittest

from tests.toolset_identity_fixture import Identity, IdentityError, RequestAliases, alias


class RequestAliasesTest(unittest.TestCase):
    def test_three_equal_names_are_distinct_and_round_trip(self):
        identities = [Identity(None, "scroll"), Identity("browser", "scroll"), Identity("computer", "scroll")]
        request = RequestAliases(identities)
        names = [request.replay_name(item) for item in identities]
        self.assertEqual(names[0], "scroll")
        self.assertEqual(len(set(names)), 3)
        self.assertEqual([request.returned_identity(name) for name in names], identities)

    def test_custom_name_cannot_impersonate_active_or_historical_toolset(self):
        member = Identity("browser", "scroll")
        custom = Identity(None, alias(member))
        with self.assertRaises(IdentityError):
            RequestAliases([member, custom])
        with self.assertRaises(IdentityError):
            RequestAliases([custom], [member])

    def test_removed_member_replays_but_cannot_be_called(self):
        member = Identity("browser", "scroll")
        request = RequestAliases([Identity(None, "other")], [member, member])
        name = request.replay_name(member)
        with self.assertRaises(IdentityError):
            request.returned_identity(name)
        with self.assertRaises(IdentityError):
            request.replay_name(Identity("computer", "scroll"))

    def test_dynamic_additions_do_not_rename_old_identities(self):
        member = Identity("browser", "scroll")
        before = RequestAliases([member])
        after = RequestAliases([Identity("computer", "scroll"), member, Identity(None, "scroll")], [member])
        self.assertEqual(before.replay_name(member), after.replay_name(member))

    def test_requests_do_not_share_offered_tools(self):
        browser = Identity("browser", "scroll")
        computer = Identity("computer", "scroll")
        first, second = RequestAliases([browser]), RequestAliases([computer])
        with self.assertRaises(IdentityError):
            first.returned_identity(alias(computer))
        with self.assertRaises(IdentityError):
            second.returned_identity(alias(browser))

    def test_invalid_unknown_long_and_duplicate_values_fail_closed(self):
        invalid = [Identity("future", "scroll"), Identity("browser", "x" * 64),
                   Identity("browser", "space name"), Identity(None, ""), Identity(None, "工具")]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(IdentityError):
                RequestAliases([item])
        with self.assertRaises(IdentityError):
            RequestAliases([Identity(None, "x"), Identity(None, "x")])
        with self.assertRaises(IdentityError):
            RequestAliases([Identity(None, "x")]).returned_identity("unknown")
        with self.assertRaises(IdentityError):
            RequestAliases([Identity(None, "x")]).returned_identity([])

    def test_small_valid_name_space_has_no_family_collisions(self):
        identities = [Identity(family, "".join(chars)) for size in range(1, 4)
                      for chars in itertools.product("aA0_-", repeat=size)
                      for family in ("browser", "computer")]
        names = [alias(item) for item in identities]
        self.assertEqual(len(names), len(set(names)))
        request = RequestAliases(identities)
        self.assertEqual([request.returned_identity(name) for name in names], identities)


if __name__ == "__main__":
    unittest.main()
