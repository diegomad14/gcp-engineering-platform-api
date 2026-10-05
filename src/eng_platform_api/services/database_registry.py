"""Bounded, digest-pinned database authority. Credentials never become DTOs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import config, validate_catalog_source
from .log_catalog import LOGIN_PATTERN


class DatabaseUnavailable(ValueError):
    """The configured authority or database cannot be trusted."""


SERVICE_PATTERN = r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
SCHEMA_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$"


def _service_names(values: list[str]) -> list[str]:
    if len(set(values)) != len(values) or any(
        not re.fullmatch(SERVICE_PATTERN, value) for value in values
    ):
        raise ValueError("Invalid database service associations")
    return sorted(values)


class TableServices(BaseModel):
    """Curated metadata; never a replacement for a live, SELECT-authorized table."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_name: str = Field(alias="schema", pattern=SCHEMA_PATTERN)
    name: str = Field(min_length=1, max_length=63)
    service_names: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def valid_table_name(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > 63:
            raise ValueError("Invalid database table association")
        return value

    @field_validator("service_names")
    @classmethod
    def valid_services(cls, values: list[str]) -> list[str]:
        return _service_names(values)


class Database(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=100)
    schemas: list[str] = Field(min_length=1, max_length=32)
    allowed_logins: list[str] = Field(min_length=1, max_length=512)
    dsn_env: str = Field(pattern=r"^ENG_PLATFORM_DATABASE_DSN_[A-Z0-9_]{1,64}$")
    service_names: list[str] = Field(default_factory=list, max_length=64)
    table_services: list[TableServices] = Field(default_factory=list, max_length=2048)

    @field_validator("service_names")
    @classmethod
    def valid_services(cls, values: list[str]) -> list[str]:
        return _service_names(values)

    @model_validator(mode="after")
    def valid_table_associations(self):
        names = [(item.schema_name, item.name) for item in self.table_services]
        if len(set(names)) != len(names) or any(
            item.schema_name not in self.schemas
            or not set(item.service_names).issubset(self.service_names)
            for item in self.table_services
        ):
            raise ValueError("Invalid database table associations")
        return self

    @field_validator("schemas")
    @classmethod
    def safe_schemas(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            not re.fullmatch(SCHEMA_PATTERN, value)
            or value.startswith("pg_")
            or value == "information_schema"
            for value in values
        ):
            raise ValueError("Invalid database schemas")
        return values

    @field_validator("allowed_logins")
    @classmethod
    def safe_logins(cls, values: list[str]) -> list[str]:
        canonical = [value.lower() for value in values]
        if len(set(canonical)) != len(canonical) or any(
            not re.fullmatch(LOGIN_PATTERN, value) for value in values
        ):
            raise ValueError("Invalid database readers")
        return sorted(canonical)

    def can_read(self, login: str) -> bool:
        return login.lower() in self.allowed_logins

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "schemas": self.schemas,
            "max_rows": 500,
            "timeout_seconds": 5,
            "service_names": self.service_names,
        }

    def services_for_table(self, schema: str, name: str) -> list[str]:
        return next(
            (
                item.service_names
                for item in self.table_services
                if (item.schema_name, item.name) == (schema, name)
            ),
            [],
        )


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    databases: list[Database] = Field(min_length=1, max_length=64)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise DatabaseUnavailable("Duplicate registry field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise DatabaseUnavailable("Invalid registry constant")


def databases() -> list[Database]:
    """Revalidate the whole private mount every time; never cache past revocation."""
    try:
        validate_catalog_source(
            config.databases.registry_path,
            config.databases.registry_sha256,
            required=True,
        )
        assert config.databases.registry_path is not None
        descriptor = os.open(
            Path(config.databases.registry_path), os.O_RDONLY | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise DatabaseUnavailable("Invalid registry mount")
            raw = source.read(262_145)
        if (
            len(raw) > 262_144
            or hashlib.sha256(raw).hexdigest() != config.databases.registry_sha256
        ):
            raise DatabaseUnavailable("Invalid registry digest")
        registry = Registry.model_validate(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
        )
        identities = [item.id for item in registry.databases]
        if len(set(identities)) != len(identities):
            raise DatabaseUnavailable("Duplicate registry identity")
        return registry.databases
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise DatabaseUnavailable("Database registry is unavailable") from None
