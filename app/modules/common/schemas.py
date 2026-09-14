import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Payload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PageQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=12, ge=1, le=100)
    q: str = Field(default="", max_length=200)


class CatalogQuery(PageQuery):
    category: str = ""
    owner: str = ""
    sort: Literal["recommended", "latest", "popular", "rating"] = "recommended"
    facets: str = Field(default="{}", max_length=8000)
    favoriteIds: str | None = Field(default=None, max_length=16000)

    def filters(self):
        try:
            facets = json.loads(self.facets)
            favorites = json.loads(self.favoriteIds) if self.favoriteIds is not None else None
            if not isinstance(facets, dict) or any(
                not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                for values in facets.values()
            ):
                raise ValueError
            if favorites is not None and (
                not isinstance(favorites, list) or any(not isinstance(v, str) for v in favorites)
            ):
                raise ValueError
            return facets, favorites
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_catalog_filters") from None


class FeedbackInput(Payload):
    type: str = Field(min_length=1, max_length=40)
    module: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=5000)
    contact: str = Field(default="", max_length=200)
    attachment_name: str = Field(default="", max_length=180)


class InvocationInput(Payload):
    prompt: str = Field(min_length=1, max_length=10000)
    format: str = ""
    version: str = ""


class AccessInput(Payload):
    purpose: str = ""
    scope: str = ""
    duration: str = ""
    description: str = Field(default="", max_length=5000)


class PublicationInput(Payload):
    kind: Literal["dataset", "model", "application", "knowledge", "tool"]
    name: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=5000)
    submit: bool = False
