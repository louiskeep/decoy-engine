"""
Relationship handling for the decoy_engine generator.
Manages relationships between tables and self-references.
"""

import os
import random
from typing import Any

import pandas as pd


class RelationshipHandler:
    """
    Handles relationships between tables and self-references.
    Ensures referential integrity in generated data.
    """

    def __init__(self, seed: int = 42, logger=None):
        """
        Initialize with a seed for deterministic behavior

        Args:
            seed: Random seed for deterministic generation
            logger: Logger instance (optional)
        """
        self.seed = seed
        random.seed(self.seed)

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

        self.logger.debug(f"Initialized RelationshipHandler with seed: {seed}")

    def process_self_references(self, table_config: dict[str, Any], config: dict[str, Any]) -> None:
        """
        Process self-referential relationships within a table

        Args:
            table_config: Configuration for the table
            config: Global configuration dictionary
        """
        table_name = table_config.get("name", "")
        self.logger.debug(f"Checking for self-references in table: {table_name}")

        # Check if this table has any self-reference relationships defined
        self_refs = []
        if "relationships" in config:
            for rel in config["relationships"]:
                if rel.get("type") == "self_reference" and rel.get("table") == table_name:
                    self_refs.append(rel)

        if not self_refs:
            self.logger.debug(f"No self-references found for table: {table_name}")
            return

        # Process each self-reference relationship
        for rel in self_refs:
            self.logger.info(
                f"Processing self-reference in table '{table_name}': {rel['column']} -> {rel['reference_column']}"
            )

            # Load the generated data
            output_path = table_config.get("output_path")
            if not output_path:
                output_dir = config.get("generator_settings", {}).get(
                    "output_directory", "data/generated/"
                )
                output_path = os.path.join(output_dir, f"{table_name}.csv")

            if not os.path.exists(output_path):
                self.logger.warning(
                    f"Cannot process self-reference: Table file not found at {output_path}"
                )
                continue

            try:
                df = pd.read_csv(output_path)

                # Get column names
                ref_column = rel["reference_column"]
                target_column = rel["column"]

                if ref_column not in df.columns or target_column not in df.columns:
                    self.logger.warning("Cannot process self-reference: Columns not found in table")
                    continue

                # Get all possible reference values
                ref_values = df[ref_column].dropna().unique().tolist()

                if not ref_values:
                    self.logger.warning(f"No reference values found in column: {ref_column}")
                    continue

                # Maximum levels of hierarchy
                max_levels = rel.get("levels", 3)

                # Assign references
                for i in range(len(df)):
                    # Skip some rows to create top-level entries (null references)
                    if i % (max_levels + 1) == 0:
                        df.at[i, target_column] = None
                        continue

                    # Find valid reference value (avoid self-references and circular references)
                    valid_refs = [r for r in ref_values if r != df.at[i, ref_column]]

                    if valid_refs:
                        # Select a random reference value
                        df.at[i, target_column] = random.choice(valid_refs)
                    else:
                        # No valid reference found, set to null
                        df.at[i, target_column] = None

                # Save the updated table
                df.to_csv(output_path, index=False)
                self.logger.info(f"Updated self-references in table: {table_name}")

            except Exception as e:
                self.logger.error(f"Error processing self-references for table {table_name}: {e!s}")

    def process_relationships(
        self, config: dict[str, Any], reference_data: dict[str, pd.DataFrame]
    ) -> None:
        """
        Process relationships between tables

        Args:
            config: Global configuration dictionary
            reference_data: Dictionary of previously generated tables
        """
        if "relationships" not in config:
            self.logger.debug("No relationships defined to process")
            return

        # Process only non-self-reference relationships
        relationships = [
            rel for rel in config.get("relationships", []) if rel.get("type") != "self_reference"
        ]

        if not relationships:
            self.logger.debug("No inter-table relationships to process")
            return

        self.logger.info(f"Processing {len(relationships)} inter-table relationships")

        # Validate dependency order before processing
        self._validate_relationship_dependencies(relationships, reference_data)

        for rel in relationships:
            rel_type = rel.get("type", "foreign_key")
            rel_name = rel.get("name", "unnamed_relationship")

            self.logger.info(f"Processing relationship: {rel_name} of type {rel_type}")

            if rel_type == "foreign_key":
                self._process_foreign_key_relationship(rel, config, reference_data, rel_name)
            elif rel_type == "many_to_many":
                self._process_many_to_many_relationship(rel, config, reference_data)
            else:
                self.logger.warning(f"Unsupported relationship type: {rel_type}")

    def _process_foreign_key_relationship(
        self,
        relationship: dict[str, Any],
        config: dict[str, Any],
        reference_data: dict[str, pd.DataFrame],
        rel_name: str,
    ) -> None:
        """
        Process a foreign key relationship between tables

        Args:
            relationship: Relationship configuration
            config: Global configuration dictionary
            reference_data: Dictionary of previously generated tables
        """
        source_table = relationship.get("source_table")
        source_column = relationship.get("source_column")
        target_table = relationship.get("target_table")
        target_column = relationship.get("target_column")

        if not all([source_table, source_column, target_table, target_column]):
            self.logger.warning("Incomplete foreign key relationship definition")
            return

        self.logger.info(
            f"Processing foreign key: {source_table}.{source_column} -> {target_table}.{target_column}"
        )

        # Check if tables are available
        if source_table not in reference_data or target_table not in reference_data:
            self.logger.warning(
                f"One or both tables ({source_table}, {target_table}) not found in reference data"
            )
            return

        source_df = reference_data[source_table]
        target_df = reference_data[target_table]

        # Check if columns exist
        if source_column not in source_df.columns or target_column not in target_df.columns:
            self.logger.warning(
                f"One or both columns ({source_column}, {target_column}) not found in tables"
            )
            return

        # Get target values
        target_values = target_df[target_column].dropna().unique().tolist()

        if not target_values:
            self.logger.warning(f"No target values found in {target_table}.{target_column}")
            return

        # Get output paths
        output_dir = config.get("generator_settings", {}).get("output_directory", "data/generated/")

        source_output_path = None
        for table_config in config.get("tables", []):
            if table_config.get("name") == source_table:
                source_output_path = table_config.get("output_path")
                break

        if not source_output_path:
            source_output_path = os.path.join(output_dir, f"{source_table}.csv")

        if not os.path.exists(source_output_path):
            self.logger.warning(f"Source table file not found at {source_output_path}")
            return

        try:
            # Load the source table
            df = pd.read_csv(source_output_path)

            # Distribution type for reference values
            distribution = relationship.get("distribution", "random")
            null_probability = relationship.get("null_probability", 0.0)

            # Update foreign key values
            for i in range(len(df)):
                # Apply null probability
                if null_probability > 0 and random.random() < null_probability:
                    df.at[i, source_column] = None
                    continue

                if distribution == "random":
                    # Random selection with replacement
                    selected_value = random.choice(target_values)
                    if pd.api.types.is_integer_dtype(df[source_column]):
                        df.at[i, source_column] = int(selected_value)
                    elif pd.api.types.is_float_dtype(df[source_column]):
                        df.at[i, source_column] = float(selected_value)
                    else:
                        df.at[i, source_column] = selected_value

                elif distribution == "sequential":
                    # Cycle through values sequentially
                    selected_value = target_values[i % len(target_values)]
                    if pd.api.types.is_integer_dtype(df[source_column]):
                        df.at[i, source_column] = int(selected_value)
                    elif pd.api.types.is_float_dtype(df[source_column]):
                        df.at[i, source_column] = float(selected_value)
                    else:
                        df.at[i, source_column] = selected_value

                elif distribution == "weighted":
                    # If weights are provided, use them
                    weights = relationship.get("weights")
                    if not weights or len(weights) != len(target_values):
                        # Default to equal weights
                        weights = None
                    elif abs(sum(weights) - 1.0) > 1e-6:  # Allow for small floating point errors
                        self.logger.warning(
                            f"Weights for relationship '{rel_name}' do not sum to 1.0 (sum: {sum(weights):.6f}). Using equal weights instead."
                        )
                        weights = None
                    selected_value = random.choices(target_values, weights=weights, k=1)[0]
                    # Ensure type compatibility with the DataFrame column
                    if pd.api.types.is_integer_dtype(df[source_column]):
                        df.at[i, source_column] = int(selected_value)
                    elif pd.api.types.is_float_dtype(df[source_column]):
                        df.at[i, source_column] = float(selected_value)
                    else:
                        df.at[i, source_column] = selected_value

                else:
                    selected_value = random.choice(target_values)
                    if pd.api.types.is_integer_dtype(df[source_column]):
                        df.at[i, source_column] = int(selected_value)
                    elif pd.api.types.is_float_dtype(df[source_column]):
                        df.at[i, source_column] = float(selected_value)
                    else:
                        df.at[i, source_column] = selected_value

            # Save the updated table
            df.to_csv(source_output_path, index=False)
            self.logger.info(f"Updated foreign key references in table: {source_table}")

        except Exception as e:
            self.logger.error(f"Error processing foreign key relationship: {e!s}")

    def _process_many_to_many_relationship(
        self,
        relationship: dict[str, Any],
        config: dict[str, Any],
        reference_data: dict[str, pd.DataFrame],
        rel_name: str,
    ) -> None:
        """
        Process a many-to-many relationship between tables

        Args:
            relationship: Relationship configuration
            config: Global configuration dictionary
            reference_data: Dictionary of previously generated tables
        """
        junction_table = relationship.get("junction_table")
        left_table = relationship.get("left_table")
        left_column = relationship.get("left_column")
        right_table = relationship.get("right_table")
        right_column = relationship.get("right_column")

        if not all([junction_table, left_table, left_column, right_table, right_column]):
            self.logger.warning(
                "Incomplete many-to-many relationship definition. Required: junction_table, left_table, left_column, right_table, right_column"
            )
            return

        self.logger.info(
            f"Processing many-to-many relationship: {left_table} <- {junction_table} -> {right_table}"
        )

        # Check if all tables exist
        if junction_table not in reference_data:
            self.logger.warning(f"Junction table '{junction_table}' not found in generated data")
            return
        if left_table not in reference_data:
            self.logger.warning(f"Left table '{left_table}' not found in generated data")
            return
        if right_table not in reference_data:
            self.logger.warning(f"Right table '{right_table}' not found in generated data")
            return

        # Get the data
        junction_df = reference_data[junction_table]
        left_df = reference_data[left_table]
        right_df = reference_data[right_table]

        # Check if junction table has the required columns
        if left_column not in junction_df.columns:
            self.logger.warning(
                f"Left column '{left_column}' not found in junction table '{junction_table}'"
            )
            return
        if right_column not in junction_df.columns:
            self.logger.warning(
                f"Right column '{right_column}' not found in junction table '{junction_table}'"
            )
            return

        # Get available values from both sides
        left_values = left_df[left_column].dropna().unique().tolist()
        right_values = right_df[right_column].dropna().unique().tolist()

        if not left_values or not right_values:
            self.logger.warning(
                f"No values found in left table '{left_table}' column '{left_column}' or right table '{right_table}' column '{right_column}'"
            )
            return

        # Generate many-to-many relationships
        # Use configurable parameters for relationship density
        left_cardinality = relationship.get("left_cardinality", "many")  # 'one' or 'many'
        right_cardinality = relationship.get("right_cardinality", "many")  # 'one' or 'many'
        max_relationships = relationship.get("max_relationships")

        # Generate relationships based on cardinality
        relationships = []

        if left_cardinality == "one" and right_cardinality == "one":
            # One-to-one: each left item connects to exactly one right item
            min_count = min(len(left_values), len(right_values))
            for i in range(min_count):
                relationships.append((left_values[i], right_values[i]))

        elif left_cardinality == "one" and right_cardinality == "many":
            # One-to-many: each left item connects to multiple right items
            for left_val in left_values:
                # Determine how many right items this left item should connect to
                num_connections = min(
                    random.randint(1, min(5, len(right_values))), len(right_values)
                )
                connected_right = random.sample(right_values, num_connections)
                for right_val in connected_right:
                    relationships.append((left_val, right_val))

        elif left_cardinality == "many" and right_cardinality == "one":
            # Many-to-one: multiple left items connect to each right item
            for right_val in right_values:
                # Determine how many left items should connect to this right item
                num_connections = min(random.randint(1, min(5, len(left_values))), len(left_values))
                connected_left = random.sample(left_values, num_connections)
                for left_val in connected_left:
                    relationships.append((left_val, right_val))

        else:  # many-to-many
            # Many-to-many: create a reasonable number of relationships
            total_possible = len(left_values) * len(right_values)
            if max_relationships:
                num_relationships = min(max_relationships, total_possible)
            else:
                # Default to about 20-50% of possible relationships
                num_relationships = min(
                    int(total_possible * random.uniform(0.2, 0.5)), total_possible
                )

            # Generate random relationships
            for _ in range(num_relationships):
                left_val = random.choice(left_values)
                right_val = random.choice(right_values)
                relationships.append((left_val, right_val))

        # Remove duplicates
        relationships = list(set(relationships))

        # Apply relationships to junction table
        junction_rows = len(junction_df)
        if len(relationships) > junction_rows:
            self.logger.warning(
                f"Generated {len(relationships)} relationships but junction table only has {junction_rows} rows. Truncating relationships."
            )
            relationships = relationships[:junction_rows]

        # Shuffle relationships to avoid patterns
        random.shuffle(relationships)

        # Update junction table
        for i, (left_val, right_val) in enumerate(relationships):
            if i < junction_rows:
                junction_df.at[i, left_column] = left_val
                junction_df.at[i, right_column] = right_val

        # Save the updated junction table
        output_dir = config.get("generator_settings", {}).get("output_directory", "data/generated/")
        junction_output_path = None
        for table_config in config.get("tables", []):
            if table_config.get("name") == junction_table:
                junction_output_path = table_config.get("output_path")
                break

        if not junction_output_path:
            junction_output_path = os.path.join(output_dir, f"{junction_table}.csv")

        junction_df.to_csv(junction_output_path, index=False)
        self.logger.info(
            f"Generated {len(relationships)} many-to-many relationships in junction table '{junction_table}'"
        )

    def _validate_relationship_dependencies(
        self, relationships: list[dict[str, Any]], reference_data: dict[str, pd.DataFrame]
    ) -> None:
        """
        Validate that all referenced tables exist before processing relationships

        Args:
            relationships: List of relationship configurations
            reference_data: Dictionary of generated tables
        """
        missing_tables = set()

        for rel in relationships:
            rel_type = rel.get("type", "foreign_key")
            rel_name = rel.get("name", "unnamed_relationship")

            if rel_type == "foreign_key":
                source_table = rel.get("source_table")
                target_table = rel.get("target_table")

                if source_table and source_table not in reference_data:
                    missing_tables.add(source_table)
                if target_table and target_table not in reference_data:
                    missing_tables.add(target_table)

            elif rel_type == "many_to_many":
                junction_table = rel.get("junction_table")
                if junction_table and junction_table not in reference_data:
                    missing_tables.add(junction_table)

        if missing_tables:
            error_msg = f"Relationship validation failed: The following tables are referenced in relationships but were not found in generated data: {', '.join(missing_tables)}. Ensure tables with dependencies are listed after their referenced tables in the configuration."
            self.logger.error(error_msg)
            raise ValueError(error_msg)
