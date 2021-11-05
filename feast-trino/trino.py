from datetime import datetime, date
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd
import pyarrow
import uuid
from feast.data_source import DataSource
from feast.errors import InvalidEntityType
from feast.feature_view import DUMMY_ENTITY_ID, DUMMY_ENTITY_VAL, FeatureView
from feast.infra.offline_stores import offline_utils
from feast.infra.offline_stores.offline_store import OfflineStore, RetrievalJob
from feast.on_demand_feature_view import OnDemandFeatureView
from feast.registry import Registry
from feast.repo_config import FeastConfigBaseModel, RepoConfig
from pydantic import StrictStr
from pydantic.typing import Literal

from feast_trino.trino_source import TrinoSource
from feast_trino.trino_utils import Query, Trino


class TrinoOfflineStoreConfig(FeastConfigBaseModel):
    """ Online store config for Trino """

    type: Literal["trino"] = "trino"
    """ Offline store type selector """

    host: StrictStr
    """ Host of the Trino cluster """

    port: int
    """ Port of the Trino cluster """

    catalog: StrictStr
    """ Catalog of the Trino cluster """

    dataset: StrictStr = "feast"
    """ (optional) Trino Dataset name for temporary tables """


class TrinoRetrievalJob(RetrievalJob):
    def __init__(
        self,
        query: Query,
        client: Trino,
        config: RepoConfig,
        full_feature_names: bool,
        on_demand_feature_views: Optional[List[OnDemandFeatureView]],
    ):
        self._query = query
        self._client = client
        self._config = config
        self._full_feature_names = full_feature_names
        self._on_demand_feature_views = on_demand_feature_views

    @property
    def full_feature_names(self) -> bool:
        return self._full_feature_names

    @property
    def on_demand_feature_views(self) -> Optional[List[OnDemandFeatureView]]:
        return self._on_demand_feature_views

    def _to_df_internal(self) -> pd.DataFrame:
        """Return dataset as Pandas DataFrame synchronously including on demand transforms"""
        results = self._client.execute_query(query_text=self._query)
        return pd.DataFrame(data=results.data, columns=results.columns_names)

    def _to_arrow_internal(self) -> pyarrow.Table:
        """Return payrrow dataset as synchronously including on demand transforms"""
        results = self._client.execute_query(query_text=self._query)
        return pyarrow.table(data=results.data, columns=results.columns_names)
    
    def to_sql(self) -> str:
        """Returns the SQL query that will be executed in Trino to build the historical feature table"""
        return self._query

    def to_trino(
            self,
            timeout: int = 1800,
            retry_cadence: int = 10,
        ) -> Optional[str]:
        """
        Triggers the execution of a historical feature retrieval query and exports the results to a Trino table.
        Runs for a maximum amount of time specified by the timeout parameter (defaulting to 30 minutes).
        
        Args:
            timeout: An optional number of seconds for setting the time limit of the QueryJob.
            retry_cadence: An optional number of seconds for setting how long the job should checked for completion.
        Returns:
            Returns the destination table name.
        """
        today = date.today().strftime("%Y%m%d")
        rand_id = str(uuid.uuid4())[:7]
        destination_table = f"{self._client.project}.{self._config.offline_store.dataset}.historical_{today}_{rand_id}"
        query = f"CREATE TABLE {destination_table} AS ({self._query})"
        self._client.execute_query(query_text=query)
        return destination_table


class TrinoOfflineStore(OfflineStore):
    @staticmethod
    def pull_latest_from_table_or_query(
        config: RepoConfig,
        data_source: DataSource,
        join_key_columns: List[str],
        feature_name_columns: List[str],
        event_timestamp_column: str,
        created_timestamp_column: Optional[str],
        start_date: datetime,
        end_date: datetime,
    ) -> TrinoRetrievalJob:
        if not isinstance(data_source, TrinoSource):
            raise ValueError(f"The data_source object is not a TrinoSource but is instead '{type(data_source)}'")
        if not isinstance(config.offline_store, TrinoOfflineStoreConfig):
            raise ValueError(f"The config.offline_store object is not a TrinoOfflineStoreConfig but is instead '{type(config.offline_store)}'")

        from_expression = data_source.get_table_query_string()

        partition_by_join_key_string = ", ".join(join_key_columns)
        if partition_by_join_key_string != "":
            partition_by_join_key_string = (
                "PARTITION BY " + partition_by_join_key_string
            )
        timestamp_columns = [event_timestamp_column]
        if created_timestamp_column:
            timestamp_columns.append(created_timestamp_column)
        timestamp_desc_string = " DESC, ".join(timestamp_columns) + " DESC"
        field_string = ", ".join(
            join_key_columns + feature_name_columns + timestamp_columns
        )

        client = _get_trino_client(config=config)

        query = f"""
            SELECT
                {field_string}
                {f", {repr(DUMMY_ENTITY_VAL)} AS {DUMMY_ENTITY_ID}" if not join_key_columns else ""}
            FROM (
                SELECT {field_string},
                ROW_NUMBER() OVER({partition_by_join_key_string} ORDER BY {timestamp_desc_string}) AS _feast_row
                FROM {from_expression}
                WHERE {event_timestamp_column} BETWEEN TIMESTAMP '{start_date}' AND TIMESTAMP '{end_date}'
            )
            WHERE _feast_row = 1
            """
        # When materializing a single feature view, we don't need full feature names. On demand transforms aren't materialized
        return TrinoRetrievalJob(
            query=query,
            client=client,
            config=config,
            full_feature_names=False,
            on_demand_feature_views=None,
        )

    @staticmethod
    def get_historical_features(
        config: RepoConfig,
        feature_views: List[FeatureView],
        feature_refs: List[str],
        entity_df: Union[pd.DataFrame, str],
        registry: Registry,
        project: str,
        full_feature_names: bool = False,
    ) -> TrinoRetrievalJob:
        if not isinstance(config.offline_store, TrinoOfflineStoreConfig):
            raise ValueError("This function should be used with a TrinoOfflineStoreConfig object. Instead we have config.offline_store being '{type(config.offline_store)}'")
        
        client = _get_trino_client(config=config)

        table_reference = _get_table_reference_for_new_entity(
            catalog=config.offline_store.catalog, dataset_name=config.offline_store.dataset
        )

        entity_schema = _upload_entity_df_and_get_entity_schema(
            client=client,
            table_name=table_reference,
            entity_df=entity_df,
        )

        entity_df_event_timestamp_col = offline_utils.infer_event_timestamp_from_entity_df(entity_schema)

        expected_join_keys = offline_utils.get_expected_join_keys(project, feature_views, registry)

        offline_utils.assert_expected_columns_in_entity_df(
            entity_schema, expected_join_keys, entity_df_event_timestamp_col
        )

        # Build a query context containing all information required to template the Trino SQL query
        query_context = offline_utils.get_feature_view_query_context(
            feature_refs,
            feature_views,
            registry,
            project,
        )

        # Generate the Trino SQL query from the query context
        query = offline_utils.build_point_in_time_query(
                query_context,
                left_table_query_string=table_reference,
                entity_df_event_timestamp_col=entity_df_event_timestamp_col,
                entity_df_columns=entity_schema.keys(),
                query_template=MULTIPLE_FEATURE_VIEW_POINT_IN_TIME_JOIN,
                full_feature_names=full_feature_names,
            )

        return TrinoRetrievalJob(
            query=query,
            client=client,
            config=config,
            full_feature_names=full_feature_names,
            on_demand_feature_views=OnDemandFeatureView.get_requested_odfvs(
                feature_refs, project, registry
            ),
        )


def _get_table_reference_for_new_entity(catalog: str, dataset_name: str) -> str:
    """Gets the table_id for the new entity to be uploaded."""
    table_name = offline_utils.get_temp_entity_table_name()
    return f"{catalog}.{dataset_name}.{table_name}"


def _upload_entity_df_and_get_entity_schema(
    client: Trino,
    table_name: str,
    entity_df: Union[pd.DataFrame, str],
) -> Dict[str, np.dtype]:
    """Uploads a Pandas entity dataframe into a Trino table and returns the resulting table"""
    if type(entity_df) is str:
        client.execute_query(f"CREATE TABLE {table_name} AS ({entity_df})")

        results = client.execute_query(f"SELECT * FROM {table_name} LIMIT 1")

        limited_entity_df = pd.DataFrame(data=results.data, columns=results.columns_names)
        for col_name, col_type in results.schema.items():
            if col_type == "timestamp":
                limited_entity_df[col_name] = pd.to_datetime(limited_entity_df[col_name])
        entity_schema = dict(zip(limited_entity_df.columns, limited_entity_df.dtypes))

        return entity_schema
    elif isinstance(entity_df, pd.DataFrame):
        raise InvalidEntityType(type(entity_df))
    else:
        raise InvalidEntityType(type(entity_df))

    # Ensure that the table expires after some time
    # NotImplemented


def _get_trino_client(config: RepoConfig) -> Trino:
    client = Trino(
        user="user",
        catalog=config.offline_store.catalog,
        host=config.offline_store.host,
        port=config.offline_store.port,
    )
    return client


MULTIPLE_FEATURE_VIEW_POINT_IN_TIME_JOIN = """
/*
 Compute a deterministic hash for the `left_table_query_string` that will be used throughout
 all the logic as the field to GROUP BY the data
*/
WITH entity_dataframe AS (
    SELECT *,
        {{entity_df_event_timestamp_col}} AS entity_timestamp
        {% for featureview in featureviews %}
            {% if featureview.entities %}
            ,CONCAT(
                {% for entity in featureview.entities %}
                    CAST({{entity}} AS VARCHAR),
                {% endfor %}
                CAST({{entity_df_event_timestamp_col}} AS VARCHAR)
            ) AS {{featureview.name}}__entity_row_unique_id
            {% else %}
            ,CAST({{entity_df_event_timestamp_col}} AS VARCHAR) AS {{featureview.name}}__entity_row_unique_id
            {% endif %}
        {% endfor %}
    FROM {{ left_table_query_string }}
),
{% for featureview in featureviews %}
{{ featureview.name }}__entity_dataframe AS (
    SELECT
        {{ featureview.entities | join(', ')}}{% if featureview.entities %},{% else %}{% endif %}
        entity_timestamp,
        {{featureview.name}}__entity_row_unique_id
    FROM entity_dataframe
    GROUP BY
        {{ featureview.entities | join(', ')}}{% if featureview.entities %},{% else %}{% endif %}
        entity_timestamp,
        {{featureview.name}}__entity_row_unique_id
),
/*
 This query template performs the point-in-time correctness join for a single feature set table
 to the provided entity table.
 1. We first join the current feature_view to the entity dataframe that has been passed.
 This JOIN has the following logic:
    - For each row of the entity dataframe, only keep the rows where the `event_timestamp_column`
    is less than the one provided in the entity dataframe
    - If there a TTL for the current feature_view, also keep the rows where the `event_timestamp_column`
    is higher the the one provided minus the TTL
    - For each row, Join on the entity key and retrieve the `entity_row_unique_id` that has been
    computed previously
 The output of this CTE will contain all the necessary information and already filtered out most
 of the data that is not relevant.
*/
{{ featureview.name }}__subquery AS (
    SELECT
        {{ featureview.event_timestamp_column }} as event_timestamp,
        {{ featureview.created_timestamp_column ~ ' as created_timestamp,' if featureview.created_timestamp_column else '' }}
        {{ featureview.entity_selections | join(', ')}}{% if featureview.entity_selections %},{% else %}{% endif %}
        {% for feature in featureview.features %}
            {{ feature }} as {% if full_feature_names %}{{ featureview.name }}__{{feature}}{% else %}{{ feature }}{% endif %}{% if loop.last %}{% else %}, {% endif %}
        {% endfor %}
    FROM {{ featureview.table_subquery }}
    WHERE {{ featureview.event_timestamp_column }} <= (SELECT MAX(entity_timestamp) FROM entity_dataframe)
    {% if featureview.ttl == 0 %}{% else %}
    AND {{ featureview.event_timestamp_column }} >= (SELECT MIN(entity_timestamp) FROM entity_dataframe) - interval '{{ featureview.ttl }}' second
    {% endif %}
),
{{ featureview.name }}__base AS (
    SELECT
        subquery.*,
        entity_dataframe.entity_timestamp,
        entity_dataframe.{{featureview.name}}__entity_row_unique_id
    FROM {{ featureview.name }}__subquery AS subquery
    INNER JOIN {{ featureview.name }}__entity_dataframe AS entity_dataframe
    ON TRUE
        AND subquery.event_timestamp <= entity_dataframe.entity_timestamp
        {% if featureview.ttl == 0 %}{% else %}
        AND subquery.event_timestamp >= entity_dataframe.entity_timestamp - interval '{{ featureview.ttl }}' second
        {% endif %}
        {% for entity in featureview.entities %}
        AND subquery.{{ entity }} = entity_dataframe.{{ entity }}
        {% endfor %}
),
/*
 2. If the `created_timestamp_column` has been set, we need to
 deduplicate the data first. This is done by calculating the
 `MAX(created_at_timestamp)` for each event_timestamp.
 We then join the data on the next CTE
*/
{% if featureview.created_timestamp_column %}
{{ featureview.name }}__dedup AS (
    SELECT
        {{featureview.name}}__entity_row_unique_id,
        event_timestamp,
        MAX(created_timestamp) as created_timestamp
    FROM {{ featureview.name }}__base
    GROUP BY {{featureview.name}}__entity_row_unique_id, event_timestamp
),
{% endif %}
/*
 3. The data has been filtered during the first CTE "*__base"
 Thus we only need to compute the latest timestamp of each feature.
*/
{{ featureview.name }}__latest AS (
    SELECT
        event_timestamp,
        {% if featureview.created_timestamp_column %}created_timestamp,{% endif %}
        {{featureview.name}}__entity_row_unique_id
    FROM
    (
        SELECT *,
            ROW_NUMBER() OVER(
                PARTITION BY {{featureview.name}}__entity_row_unique_id
                ORDER BY event_timestamp DESC{% if featureview.created_timestamp_column %},created_timestamp DESC{% endif %}
            ) AS row_number
        FROM {{ featureview.name }}__base
        {% if featureview.created_timestamp_column %}
            INNER JOIN {{ featureview.name }}__dedup
            USING ({{featureview.name}}__entity_row_unique_id, event_timestamp, created_timestamp)
        {% endif %}
    )
    WHERE row_number = 1
),
/*
 4. Once we know the latest value of each feature for a given timestamp,
 we can join again the data back to the original "base" dataset
*/
{{ featureview.name }}__cleaned AS (
    SELECT base.*, {{featureview.name}}__entity_row_unique_id
    FROM {{ featureview.name }}__base as base
    INNER JOIN {{ featureview.name }}__latest
    USING(
        {{featureview.name}}__entity_row_unique_id,
        event_timestamp
        {% if featureview.created_timestamp_column %}
            ,created_timestamp
        {% endif %}
    )
){% if loop.last %}{% else %}, {% endif %}
{% endfor %}
/*
 Joins the outputs of multiple time travel joins to a single table.
 The entity_dataframe dataset being our source of truth here.
 */
SELECT {{ final_output_feature_names | join(', ')}}
FROM entity_dataframe
{% for featureview in featureviews %}
LEFT JOIN (
    SELECT
        {{featureview.name}}__entity_row_unique_id
        {% for feature in featureview.features %}
            ,{% if full_feature_names %}{{ featureview.name }}__{{feature}}{% else %}{{ feature }}{% endif %}
        {% endfor %}
    FROM {{ featureview.name }}__cleaned
) USING ({{featureview.name}}__entity_row_unique_id)
{% endfor %}
"""