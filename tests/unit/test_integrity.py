# tests/unit/test_integrity.py
"""
Unit tests for referential integrity handling.
"""

import pandas as pd
import pytest

from decoy_engine.internal.integrity import ReferentialIntegrityManager


@pytest.fixture
def related_dataframes():
    """Create related dataframes for testing referential integrity."""
    # Customer data
    customers = pd.DataFrame(
        {
            "customer_id": ["CUST001", "CUST002", "CUST003"],
            "name": ["John Doe", "Jane Smith", "Alice Johnson"],
        }
    )

    # Order data with references to customers
    orders = pd.DataFrame(
        {
            "order_id": ["ORD001", "ORD002", "ORD003", "ORD004"],
            "customer_id": ["CUST001", "CUST002", "CUST001", "CUST003"],
            "amount": [100.0, 200.0, 150.0, 300.0],
        }
    )

    # Payment data with references to orders
    payments = pd.DataFrame(
        {
            "payment_id": ["PAY001", "PAY002", "PAY003", "PAY004"],
            "order_id": ["ORD001", "ORD002", "ORD003", "ORD004"],
            "amount": [100.0, 200.0, 150.0, 300.0],
        }
    )

    return {"customers": customers, "orders": orders, "payments": payments}


def test_referential_integrity(related_dataframes, mock_logger, tmp_path):
    """Test referential integrity manager."""
    config = {
        "referential_integrity": [
            {
                "name": "customer_relation",
                "columns": ["customers.customer_id", "orders.customer_id"],
            },
            {"name": "order_relation", "columns": ["orders.order_id", "payments.order_id"]},
        ],
    }

    # Initialize manager
    manager = ReferentialIntegrityManager(config, mock_logger)

    # Test get_referential_relationship
    assert manager.get_referential_relationship("customers", "customer_id") == "customer_relation"
    assert manager.get_referential_relationship("orders", "customer_id") == "customer_relation"
    assert manager.get_referential_relationship("orders", "order_id") == "order_relation"
    assert manager.get_referential_relationship("payments", "order_id") == "order_relation"
    assert manager.get_referential_relationship("customers", "name") is None

    # Test applying the shared relationship transform to the customers table.
    rule = {"column": "customer_id", "type": "hash"}
    customers_column = related_dataframes["customers"]["customer_id"]
    masked_customers = manager.apply_relationship_transform(
        customers_column, "customer_relation", rule
    )

    # Ensure all values changed
    assert not customers_column.equals(masked_customers)

    # Apply to orders table using the same deterministic relationship name.
    orders_column = related_dataframes["orders"]["customer_id"]
    masked_orders = manager.apply_relationship_transform(orders_column, "customer_relation", rule)

    # Verify referential integrity is maintained
    # For each order, get the original customer_id
    for i, original_id in enumerate(orders_column):
        # Find the index in the customers dataframe
        customer_idx = customers_column[customers_column == original_id].index[0]

        # The masked order customer_id should match the masked customer's customer_id
        assert masked_orders[i] == masked_customers[customer_idx]

    # A fresh manager still produces the same hash output because hash is a
    # deterministic function.
    new_manager = ReferentialIntegrityManager(config, mock_logger)
    new_masked_customers = new_manager.apply_relationship_transform(
        customers_column, "customer_relation", rule
    )
    pd.testing.assert_series_equal(masked_customers, new_masked_customers)


def test_referential_integrity_categorical_is_deterministic_without_store(mock_logger, tmp_path):
    """Relationship masking should not need local state for categorical masks."""
    legacy_state_dir = tmp_path / "mappings"
    config = {
        "referential_integrity": [
            {
                "name": "shared_identity",
                "columns": ["left.external_id", "right.member_id"],
            }
        ],
    }
    manager = ReferentialIntegrityManager(config, mock_logger)

    left = pd.Series(["A1", "A2", "A1"])
    right = pd.Series(["A2", "A1"])
    left_rule = {
        "column": "external_id",
        "type": "categorical",
        "categories": ["tier_a", "tier_b", "tier_c"],
    }
    right_rule = {
        "column": "member_id",
        "type": "categorical",
        "categories": ["tier_a", "tier_b", "tier_c"],
    }

    masked_left = manager.apply_relationship_transform(left, "shared_identity", left_rule)
    masked_right = manager.apply_relationship_transform(right, "shared_identity", right_rule)

    assert masked_left.iloc[0] == masked_left.iloc[2]
    assert masked_left.iloc[0] == masked_right.iloc[1]
    assert masked_left.iloc[1] == masked_right.iloc[0]
    assert not legacy_state_dir.exists()
