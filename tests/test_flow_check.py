#!/usr/bin/env python3
"""Unit tests for flow_check.py

Tests core functions:
- hash_flow_content: Verify deterministic hashing
- parse_instance_spec: Verify instance spec parsing
- Flow content handling
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Add toolbox to path
sys.path.insert(0, str(Path(__file__).parent.parent / "toolbox"))
import flow_check as fc


class TestHashFlowContent(unittest.TestCase):
    """Test hash_flow_content function."""

    def test_hash_deterministic(self):
        """Same content should always produce same hash."""
        content = {"block": "test", "value": 123}
        hash1 = fc.hash_flow_content(json.dumps(content))
        hash2 = fc.hash_flow_content(json.dumps(content))
        self.assertEqual(hash1, hash2)

    def test_hash_different_content(self):
        """Different content should produce different hashes."""
        content1 = {"block": "test1"}
        content2 = {"block": "test2"}
        hash1 = fc.hash_flow_content(json.dumps(content1))
        hash2 = fc.hash_flow_content(json.dumps(content2))
        self.assertNotEqual(hash1, hash2)

    def test_hash_dict_input(self):
        """Should handle dict input directly and produce same hash for identical dicts."""
        content1 = {"block": "test", "value": 123}
        content2 = {"block": "test", "value": 123}
        # Same dict content should produce same hash
        hash1 = fc.hash_flow_content(content1)
        hash2 = fc.hash_flow_content(content2)
        self.assertEqual(hash1, hash2)

    def test_hash_length(self):
        """Hash should be 16 characters."""
        content = {"block": "test"}
        hash_val = fc.hash_flow_content(json.dumps(content))
        self.assertEqual(len(hash_val), 16)

    def test_hash_is_hex(self):
        """Hash should be valid hexadecimal."""
        content = {"block": "test"}
        hash_val = fc.hash_flow_content(json.dumps(content))
        try:
            int(hash_val, 16)
        except ValueError:
            self.fail(f"Hash '{hash_val}' is not valid hexadecimal")

    def test_hash_order_independence(self):
        """Hash should be same regardless of dict key order."""
        dict1 = {"a": 1, "b": 2, "c": 3}
        dict2 = {"c": 3, "b": 2, "a": 1}
        # Both should hash to same value because flow_check uses sort_keys=True internally
        hash1 = fc.hash_flow_content(dict1)
        hash2 = fc.hash_flow_content(dict2)
        self.assertEqual(hash1, hash2)


class TestParseInstanceSpec(unittest.TestCase):
    """Test parse_instance_spec function."""

    def test_spec_with_label(self):
        """Parse spec with all three components."""
        spec = "abc-123:us-east-1:prod"
        result = fc.parse_instance_spec(spec)
        self.assertEqual(result.instance_id, "abc-123")
        self.assertEqual(result.region, "us-east-1")
        self.assertEqual(result.label, "prod")

    def test_spec_without_label(self):
        """Parse spec without label, should default to region."""
        spec = "abc-123:us-east-1"
        result = fc.parse_instance_spec(spec)
        self.assertEqual(result.instance_id, "abc-123")
        self.assertEqual(result.region, "us-east-1")
        self.assertEqual(result.label, "us-east-1")

    def test_spec_with_label_as_namedtuple(self):
        """Result should be a NamedTuple with expected fields."""
        spec = "abc-123:us-west-2:staging"
        result = fc.parse_instance_spec(spec)
        self.assertTrue(hasattr(result, "instance_id"))
        self.assertTrue(hasattr(result, "region"))
        self.assertTrue(hasattr(result, "label"))

    def test_spec_invalid_format(self):
        """Invalid spec should raise ValueError."""
        spec = "only-one-part"
        with self.assertRaises(ValueError):
            fc.parse_instance_spec(spec)

    def test_spec_with_multiple_colons(self):
        """Handle UUIDs and complex IDs with multiple colons."""
        spec = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:eu-west-1:prod"
        result = fc.parse_instance_spec(spec)
        self.assertEqual(result.instance_id, "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.assertEqual(result.region, "eu-west-1")
        self.assertEqual(result.label, "prod")


class TestFlowInfo(unittest.TestCase):
    """Test FlowInfo NamedTuple."""

    def test_flow_info_creation(self):
        """FlowInfo should be creatable with required fields."""
        flow = fc.FlowInfo(
            name="Main IVR",
            id="flow-123",
            content_hash="abcdef1234567890",
            arn="arn:aws:connect:us-east-1:123456789012:instance/xxx/contact-flow/yyy"
        )
        self.assertEqual(flow.name, "Main IVR")
        self.assertEqual(flow.id, "flow-123")
        self.assertEqual(len(flow.content_hash), 16)


class TestHashConsistency(unittest.TestCase):
    """Test hash consistency across different serializations."""

    def test_empty_object(self):
        """Empty object should have consistent hash."""
        h1 = fc.hash_flow_content({})
        h2 = fc.hash_flow_content(json.dumps({}))
        self.assertEqual(h1, h2)

    def test_nested_structure(self):
        """Complex nested structure should hash consistently."""
        content = {
            "Actions": [
                {"Identifier": "a1", "Type": "MessageParticipant"},
                {"Identifier": "a2", "Type": "DisconnectParticipant"}
            ],
            "StartAction": "a1"
        }
        h1 = fc.hash_flow_content(json.dumps(content))
        h2 = fc.hash_flow_content(json.dumps(content))
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
