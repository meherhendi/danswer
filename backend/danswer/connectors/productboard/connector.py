from collections.abc import Generator
from itertools import chain
from typing import Any
from typing import cast

import requests
from bs4 import BeautifulSoup
from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.utils.logger import setup_logger
from dateutil import parser
from retry import retry


logger = setup_logger()


_PRODUCT_BOARD_BASE_URL = "https://api.productboard.com"


class ProductboardApiError(Exception):
    pass


class ProductboardConnector(PollConnector):
    def __init__(
        self,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.batch_size = batch_size
        self.access_token: str | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.access_token = credentials["productboard_access_token"]
        return None

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Version": "1",
        }

    @staticmethod
    def _parse_description_html(description_html: str) -> str:
        soup = BeautifulSoup(description_html, "html.parser")
        return soup.get_text()

    @staticmethod
    def _get_owner_email(productboard_obj: dict[str, Any]) -> str | None:
        owner_dict = cast(dict[str, str] | None, productboard_obj.get("owner"))
        if not owner_dict:
            return None
        return owner_dict.get("email")

    def _fetch_documents(
        self,
        initial_link: str,
    ) -> Generator[dict[str, Any], None, None]:
        headers = self._build_headers()

        @retry(tries=3, delay=1, backoff=2)
        def fetch(link: str) -> dict[str, Any]:
            response = requests.get(link, headers=headers)
            if not response.ok:
                # rate-limiting is at 50 requests per second.
                # The delay in this retry should handle this while this is
                # not parallelized.
                raise ProductboardApiError(
                    "Failed to fetch from productboard - status code:"
                    f" {response.status_code} - response: {response.text}"
                )

            return response.json()

        curr_link = initial_link
        while True:
            response_json = fetch(curr_link)
            for entity in response_json["data"]:
                yield entity

            curr_link = response_json.get("links", {}).get("next")
            if not curr_link:
                break

    def _get_features(self) -> Generator[Document, None, None]:
        """A Feature is like a ticket in Jira"""
        for feature in self._fetch_documents(
            initial_link=f"{_PRODUCT_BOARD_BASE_URL}/features"
        ):
            yield Document(
                id=feature["id"],
                sections=[
                    Section(
                        link=feature["links"]["html"],
                        text=" - ".join(
                            (
                                feature["name"],
                                self._parse_description_html(feature["description"]),
                            )
                        ),
                    )
                ],
                semantic_identifier=feature["name"],
                source=DocumentSource.PRODUCTBOARD,
                metadata={
                    "productboard_entity_type": feature["type"],
                    "status": feature["status"]["name"],
                    "owner": self._get_owner_email(feature),
                    "updated_at": feature["updatedAt"],
                },
            )

    def _get_components(self) -> Generator[Document, None, None]:
        """A Component is like an epic in Jira. It contains Features"""
        for component in self._fetch_documents(
            initial_link=f"{_PRODUCT_BOARD_BASE_URL}/components"
        ):
            yield Document(
                id=component["id"],
                sections=[
                    Section(
                        link=component["links"]["html"],
                        text=" - ".join(
                            (
                                component["name"],
                                self._parse_description_html(component["description"]),
                            )
                        ),
                    )
                ],
                semantic_identifier=component["name"],
                source=DocumentSource.PRODUCTBOARD,
                metadata={
                    "productboard_entity_type": "component",
                    "owner": self._get_owner_email(component),
                    "updated_at": component["updatedAt"],
                },
            )

    def _get_products(self) -> Generator[Document, None, None]:
        """A Product is the highest level of organization.
        A Product contains components, which contains features."""
        for product in self._fetch_documents(
            initial_link=f"{_PRODUCT_BOARD_BASE_URL}/products"
        ):
            yield Document(
                id=product["id"],
                sections=[
                    Section(
                        link=product["links"]["html"],
                        text=" - ".join(
                            (
                                product["name"],
                                self._parse_description_html(product["description"]),
                            )
                        ),
                    )
                ],
                semantic_identifier=product["name"],
                source=DocumentSource.PRODUCTBOARD,
                metadata={
                    "productboard_entity_type": "product",
                    "owner": self._get_owner_email(product),
                    "updated_at": product["updatedAt"],
                },
            )

    def _get_objectives(self) -> Generator[Document, None, None]:
        for objective in self._fetch_documents(
            initial_link=f"{_PRODUCT_BOARD_BASE_URL}/objectives"
        ):
            yield Document(
                id=objective["id"],
                sections=[
                    Section(
                        link=objective["links"]["html"],
                        text=" - ".join(
                            (
                                objective["name"],
                                self._parse_description_html(objective["description"]),
                            )
                        ),
                    )
                ],
                semantic_identifier=objective["name"],
                source=DocumentSource.PRODUCTBOARD,
                metadata={
                    "productboard_entity_type": "release",
                    "state": objective["state"],
                    "owner": self._get_owner_email(objective),
                    "updated_at": objective["updatedAt"],
                },
            )

    def _is_updated_at_out_of_time_range(
        self,
        document: Document,
        start: SecondsSinceUnixEpoch,
        end: SecondsSinceUnixEpoch,
    ) -> bool:
        updated_at = cast(str, document.metadata.get("updated_at", ""))
        if updated_at:
            updated_at_datetime = parser.parse(updated_at)
            if (
                updated_at_datetime.timestamp() < start
                or updated_at_datetime.timestamp() > end
            ):
                return True
        else:
            logger.error(f"Unable to find updated_at for document '{document.id}'")

        return False

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        if self.access_token is None:
            raise PermissionError(
                "Access token is not set up, was load_credentials called?"
            )

        document_batch: list[Document] = []

        # NOTE: there is a concept of a "Note" in productboard, however
        # there is no read API for it atm. Additionally, comments are not
        # included with features. Finally, "Releases" are not fetched atm,
        # since they do not provide an updatedAt.
        feature_documents = self._get_features()
        component_documents = self._get_components()
        product_documents = self._get_products()
        objective_documents = self._get_objectives()
        for document in chain(
            feature_documents,
            component_documents,
            product_documents,
            objective_documents,
        ):
            # skip documents that are not in the time range
            if self._is_updated_at_out_of_time_range(document, start, end):
                continue

            document_batch.append(document)
            if len(document_batch) >= self.batch_size:
                yield document_batch
                document_batch = []

        if document_batch:
            yield document_batch
