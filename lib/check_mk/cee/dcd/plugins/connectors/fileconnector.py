# -*- encoding: utf-8; py-indent-offset: 4 -*-

# +------------------------------------------------------------+
# |                                                            |
# |             | |             | |            | |             |
# |          ___| |__   ___  ___| | ___ __ ___ | | __          |
# |         / __| '_ \ / _ \/ __| |/ / '_ ` _ \| |/ /          |
# |        | (__| | | |  __/ (__|   <| | | | | |   <           |
# |         \___|_| |_|\___|\___|_|\_\_| |_| |_|_|\_\          |
# |                                   custom code by SVA       |
# |                                                            |
# +------------------------------------------------------------+
#
# File Connector is a no-code DCD connector for checkmk.
#
# Copyright (C) 2021-2024 SVA System Vertrieb Alexander GmbH
#                         Niko Wenselowski <niko.wenselowski@sva.de>
#                         Jeronimo Wiederhold <jeronimo.wiederhold@sva.de>

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""
File Connector import logic.
"""

import csv
import dataclasses
import json
import re
import time
from abc import ABC, abstractmethod
from functools import partial, wraps
from itertools import zip_longest
from typing import Dict, Iterable, List, Optional, Self, Set, Tuple

from cmk.ccc.i18n import _  # pylint: disable=import-error

from cmk.utils.global_ident_type import GlobalIdent

from cmk.cee.dcd.config import ConnectorConfigModel
from cmk.cee.dcd.connector_api import (
    ConnectorObject,
    Phase1Result,
)
from cmk.cee.dcd.connector_backend import (
    Connector,
    connector_registry,
    NullObject,
)
from cmk.cee.dcd.site_api import MKAPIError  # pylint: disable=import-error

try:
    from functools import cache  # pylint: disable=ungrouped-imports
except ImportError:
    from functools import lru_cache

    cache = lru_cache(maxsize=None)

BUILTIN_ATTRIBUTES = {"locked_by", "labels", "meta_data"}
IP_ATTRIBUTES = {"ipv4", "ip", "ipaddress"}
FOLDER_PLACEHOLDER = "undefined"
PATH_SEPERATOR = "/"
REPLACABLE_CHARS = "äöüÄÖÜ(),"
REPLACEMENT_CHAR = "_"


def normalize_hostname(hostname: str) -> str:
    "Generate a normalized hostname form"
    return hostname.lower().replace(" ", "_")


@cache
def sanitise_str(value) -> str:  # type: ignore[no-untyped-def]
    "Remove characters that can cause trouble with REST API"
    for char in REPLACABLE_CHARS:
        value = value.replace(char, REPLACEMENT_CHAR)

    return value


def get_host_label(host: Dict[str, str], hostname_field: str) -> Dict[str, str]:
    """
    Get the labels from a host.

    Labels are either prefixed with "label_" or are not any of the
    known values for IPs.
    """

    def unlabelify(value: str) -> str:
        if value.startswith("label_"):
            return value[6:]

        return value

    # more logic, so no dict comprehension for readability
    # tmp = {key.lower(): value for key, value in host.items() if key != hostname_field}

    tmp = {}
    for key, value in host.items():
        if key == hostname_field:
            continue

        if ":sep(" in key:
            key, sep = re.findall(r"(.*):sep\((.*)\)", key)[0]
            values = value.split(sep)
            for value in values:
                tmp[f"{key}/{value}".lower()] = "true"
            continue

        tmp[key.lower()] = value

    return {
        unlabelify(key): value
        for key, value in tmp.items()
        if not (
            is_tag(key)
            or key in IP_ATTRIBUTES  # noqa: W503
            or is_attribute(key)  # noqa: W503
            or key in BUILTIN_ATTRIBUTES  # noqa: W503
        )
    }


def get_host_attributes(host: Dict[str, str]) -> Dict[str, str]:
    "Get unprefixed host attributes from the given dict."

    def unprefix(value: str) -> str:
        # Because we use is_attribute we can be sure that every value
        # we receive is prefixed with `attr_`
        return value[5:]

    return {
        unprefix(key): value
        for key, value in host.items()
        if is_attribute(key) and unprefix(key) not in BUILTIN_ATTRIBUTES
    }


def is_attribute(string: str) -> bool:
    "Checks if a field is marked as attribute."
    return string.lower().startswith("attr_")


def get_ip_address(host: Dict[str, str]) -> Optional[str]:
    """
    Tries to get an IP address for a host. If not found returns `None`.

    If multiple IPs are given and separated through a comma only the
    first IP address will be used.
    """

    for field in IP_ATTRIBUTES:
        try:
            ip_address = host[field].split(",")[0]  # use only first IP
        except KeyError:
            continue

        return ip_address.strip()

    return None


def fields_contain_ip_addresses(fields: List[str]) -> bool:
    "Do these fields contain IP address fields?"
    for item in fields:
        if item in IP_ATTRIBUTES:
            return True

    return False


def get_host_tags(attributes: Dict[str, str]) -> Dict[str, str]:
    "Get attributes of the host from the given dict"
    return {attr: value for attr, value in attributes.items() if is_tag(attr)}


def is_tag(name: str) -> bool:
    """
    Is the name a 'tag'?

    Checks for attributes that begin 'tag_' as this is how the
    CMK API handles this cases.
    """
    return name.lower().startswith("tag_")


def create_hostlike_tags(tags_from_cmk: dict) -> Dict[str, List[str]]:
    """
    Create tags in a format that is similar to the ones
    present at hosts.

    Tags at a host are prefixed with 'tag_'
    """
    return {"tag_" + tag["id"]: [choice["id"] for choice in tag["tags"]] for tag in tags_from_cmk}


@dataclasses.dataclass
class FileConnectorConfig:
    """Loading the persisted connection config"""

    site: str
    disabled: bool
    interval: int
    path: str
    file_format: str
    folder: str
    lowercase_everything: bool
    replace_special_chars: bool
    host_filters: List[str]
    host_overtake_filters: List[str]
    chunk_size: int
    use_service_discovery: bool
    label_path_template: str
    csv_delimiter: str | None
    label_prefix: str | None

    @classmethod
    def name(cls) -> str:  # pylint: disable=missing-function-docstring
        return "fileconnector"

    @classmethod
    def load(cls, all_cfg: ConnectorConfigModel) -> Self:
        connector_cfg = all_cfg.connector.config
        return cls(
            site=all_cfg.site,
            disabled=all_cfg.disabled,
            interval=connector_cfg["interval"],
            path=connector_cfg["path"],
            file_format=connector_cfg.get("file_format", "csv"),
            folder=connector_cfg["folder"],
            lowercase_everything=connector_cfg.get("lowercase_everything", False),
            replace_special_chars=connector_cfg.get("replace_special_chars", False),
            host_filters=connector_cfg.get("host_filters", []),
            host_overtake_filters=connector_cfg.get("host_overtake_filters", []),
            chunk_size=connector_cfg.get("chunk_size", 0),
            use_service_discovery=connector_cfg.get("use_service_discovery", True),
            label_path_template=connector_cfg.get("label_path_template", ""),
            csv_delimiter=connector_cfg.get("csv_delimiter"),
            label_prefix=connector_cfg.get("label_prefix"),
        )


class FileImporter(ABC):  # pylint: disable=too-few-public-methods
    "Basic file importer"

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.hosts: Optional[list] = None
        self.fields: Optional[List[str]] = None
        self.hostname_field: Optional[str] = None

    @abstractmethod
    def import_hosts(self):
        "This function will be called for importing the hosts."


class CSVImporter(FileImporter):  # pylint: disable=too-few-public-methods
    "Import hosts from a CSV file"

    def __init__(self, filepath: str, delimiter: str | None = None):
        super().__init__(filepath)

        self.delimiter = delimiter

    def import_hosts(self):
        with open(self.filepath) as cmdb_export:  # pylint: disable=unspecified-encoding
            if self.delimiter:
                reader = csv.DictReader(cmdb_export, delimiter=self.delimiter)
            else:
                reader = csv.DictReader(cmdb_export)

            self.hosts = list(reader)
            self.fields = reader.fieldnames  # type: ignore[assignment]

        try:
            # We always assume that the first column in our CSV is the hostname
            if self.fields is not None:
                self.hostname_field = self.fields[0]
        except IndexError:
            # Handling the error will be done in the calling method
            pass


class JSONImporter(FileImporter):  # pylint: disable=too-few-public-methods
    "Import hosts from a file with JSON"

    EXPECTED_HOST_NAMES = [
        "name",
        "hostname",
    ]

    def import_hosts(self):
        with open(self.filepath) as export_file:  # pylint: disable=unspecified-encoding
            self.hosts = json.load(export_file)

        fields = set()
        if self.hosts is not None:
            for host in self.hosts:
                fields.update(host.keys())

        self.fields = list(fields)

        possible_hostname_fields = self.EXPECTED_HOST_NAMES + list(IP_ATTRIBUTES)
        for field in possible_hostname_fields:
            if field in self.fields:
                self.hostname_field = field
                break


class BVQImporter(FileImporter):
    "Import hosts from a BVQ file"

    FIELD_MAPPING = (
        # Mapping data from CMK to JSON.
        # (CMK, JSON)
        ("label_bvq_type", "tag"),
        ("ipv4", "ipv4"),
        ("ipv6", "ipv6"),
    )

    def __init__(self, filepath: str):
        super().__init__(filepath)

        # We know that this is our field
        self.hostname_field = "name"

    def import_hosts(self):
        with open(self.filepath) as export_file:  # pylint: disable=unspecified-encoding
            hosts = json.load(export_file)

        self.hosts = [
            self.format_host(element["hostAddress"])
            for element in hosts
            if "hostAddress" in element
        ]

        fields = set()
        for host in self.hosts:
            fields.update(host.keys())

        self.fields = list(fields)

    @classmethod
    def format_host(cls, host: dict):  # type: ignore[no-untyped-def]
        "Get a host object formatted as required for further processing"
        # BVQ sends more fields than we handle.
        # We currently exclude:
        #  - masterGroupingObjectIpv4
        #  - masterGroupingObjectIpv6

        new_host = {"name": host["name"]}

        for host_key, json_key in cls.FIELD_MAPPING:
            try:
                new_host[host_key] = host[json_key]
            except KeyError:
                continue

        return new_host


class ModifyingImporter:
    "Base class that allows modifying data retrieved from an importer."

    def __init__(self, importer):
        self._importer = importer

    @property
    def filepath(self):  # pylint: disable=missing-function-docstring
        return self._importer.filepath

    @property
    def hosts(self):  # pylint: disable=missing-function-docstring
        return self._importer.hosts

    @property
    def fields(self):  # pylint: disable=missing-function-docstring
        return self._importer.fields

    @property
    def hostname_field(self):  # pylint: disable=missing-function-docstring
        return self._importer.hostname_field

    def import_hosts(self):
        "Import hosts through the importer"
        return self._importer.import_hosts()


class LowercaseImporter(ModifyingImporter):
    "This modifies an importer to only return lowercased values"

    @property
    def hosts(self):  # pylint: disable=missing-function-docstring
        hosts = self._importer.hosts
        if hosts is None:
            return None

        lowercase = self.lowercase

        def lowercase_host(host):
            return {key.lower(): lowercase(value) for key, value in host.items()}

        return [lowercase_host(host) for host in hosts]

    @property
    def fields(self):  # pylint: disable=missing-function-docstring
        fields = self._importer.fields
        if fields is None:
            return None

        return [self.lowercase(fieldname) for fieldname in fields]

    @property
    def hostname_field(self):  # pylint: disable=missing-function-docstring
        hostname_field = self._importer.hostname_field
        if hostname_field is None:
            return None

        return hostname_field.lower()

    @staticmethod
    def lowercase(value):
        "Convert the given value to lowercase if possible"
        if isinstance(value, (int, float, bool)):
            return value

        return value.lower()


class SanitisingImporter(ModifyingImporter):
    """
    This modifies an importer to return sanitised values.

    Sanitised values are required because the checkmk REST API does not accept
    some characters in the object values.
    The HTTP API did accept these before.
    """

    @property
    def hosts(self):  # pylint: disable=missing-function-docstring
        hosts = self._importer.hosts
        if hosts is None:
            return None

        sanitise = self.sanitise

        def sanitise_host(host):
            return {key: sanitise(value) for key, value in host.items()}

        return [sanitise_host(host) for host in hosts]

    @staticmethod
    def sanitise(value):
        "Convert the given value to lowercase if possible"
        if isinstance(value, (int, float, bool)):
            return value

        return sanitise_str(value)


class BaseApiClient(ABC):
    "Abstract class as a base for creating new API clients"

    def __init__(self, api_client):
        self._api_client = api_client

    @property
    def api_supports_tags(self) -> bool:
        "Indicates if the used api supports retrieving host tags"
        return True

    @abstractmethod
    def get_hosts(self) -> List[dict]:
        "Retrieve the existing hosts"

    @abstractmethod
    def add_hosts(self, hosts: List[dict]) -> Dict:
        """
        Add new hosts

        The returned dict has two keys:
          - failed_hosts
          - succeeded_hosts

        The "failed_hosts" are a dict with hostname as the key and
        additonal information about the problem as the value.
        The "succeeded_hosts" are a list of successfully processed
        hostnames.
        """

    @abstractmethod
    def modify_hosts(self, hosts: List[tuple]) -> Dict:
        """
        Modify existing hosts

        The returned dict has two keys:
          - failed_hosts
          - succeeded_hosts

        The "failed_hosts" are a dict with hostname as the key and
        additonal information about the problem as the value.
        The "succeeded_hosts" are a list of successfully processed
        hostnames.
        """

    @abstractmethod
    def delete_hosts(self, hosts: List[dict]):  # type: ignore[no-untyped-def]
        "Delete existing hosts"

    @abstractmethod
    def move_host(self, host: str, path: str):  # type: ignore[no-untyped-def]
        "Move an existing host to a new path"

    @abstractmethod
    def get_host_tags(self) -> List[dict]:
        """
        Retrieve the existing host tags.

        This includes builtin and custom created tag groups.
        Auxiliary tags are not included.
        """

    @abstractmethod
    def discover_services(self, hostnames: List[str]):  # type: ignore[no-untyped-def]
        "Trigger a service discovery on the given hosts"

    @abstractmethod
    def is_discovery_running(self) -> bool:
        "Checks if discovery is currently running"

    @abstractmethod
    def activate_changes(self) -> bool:
        "Activate pending changes"

    @property
    def requires_activation(self) -> bool:
        """
        Indicates if the class requires an explicit activation after
        making changes.
        """
        return True

    @abstractmethod
    def get_folders_from_new_hosts(self, hosts: List[dict]) -> Set[str]:
        "Get the folders from the hosts to create."

    @abstractmethod
    def get_folders(self) -> Set[str]:
        "Retrieve existing folders"

    @abstractmethod
    def add_folder(self, folder: str):  # type: ignore[no-untyped-def]
        "Add new folder"


class HttpApiClient(BaseApiClient):
    "A client that uses the legacy HTTP API of checkmk"

    def get_hosts(self) -> List[dict]:
        return self._api_client.get_all_hosts()

    def add_hosts(self, hosts: List[dict]) -> Dict:
        return self._api_client.add_hosts(hosts)

    def move_host(self, host: str, path: str) -> tuple:
        return self._api_client.move_host(host, path)

    def modify_hosts(self, hosts: List[tuple]) -> Dict:
        cleaned_hosts = self._remove_meta_data(hosts)
        return self._api_client.edit_hosts(cleaned_hosts)

    @classmethod
    def _remove_meta_data(cls, hosts: List[tuple]) -> List[tuple]:
        """
        Remove the meta_data field from host attributes to update.

        Since checkmk 2.1 the API will throw an error if the attributes to
        update contain the field "meta_data".
        Therefore we remove this field.
        """
        cleaned_hosts = []
        for hostname, update_attributes, delete_attributes in hosts:
            try:
                del update_attributes["meta_data"]
            except KeyError:
                pass
            cleaned_hosts.append((hostname, update_attributes, delete_attributes))

        return cleaned_hosts

    def delete_hosts(self, hosts: List[dict]) -> None:
        self._api_client.delete_hosts(hosts)

    def get_host_tags(self) -> List[dict]:
        # Working around limitations of the builtin client to get the
        # required results from the API.
        # The second parameter has to be a dict.
        tag_response = self._api_client._api_request(  # pylint: disable=protected-access
            "webapi.py?action=get_hosttags", {}
        )

        # The response contains a dict with the keys
        # aux_tags, builtin, tag_groups and configuration_hash.
        # Each tag has an field 'id' field we use for matching.
        all_tags = tag_response["tag_groups"]  # a list
        all_tags.extend(tag_response["builtin"]["tag_groups"])

        return all_tags

    def discover_services(self, hostnames: List[str]) -> None:
        self._api_client.bulk_discovery_start(hostnames)

    def is_discovery_running(self) -> bool:
        return self._api_client.bulk_discovery_status()["is_active"]

    def activate_changes(self) -> bool:
        try:
            self._api_client.activate_changes()
        except MKAPIError as error:
            if "no changes to activate" in str(error):
                return False

            raise

        return True

    def get_folders_from_new_hosts(self, hosts: List[dict]) -> Set[str]:
        "Get the folders from the hosts to create."
        return {folder_path for (_, folder_path, _) in hosts}

    def get_folders(self) -> Set[str]:
        all_folders = self._api_client._api_request(  # pylint: disable=protected-access
            "webapi.py?action=get_all_folders", {}
        )
        return set(all_folders)

    def add_folder(self, folder: str):  # type: ignore[no-untyped-def]
        # Follow the required format for the request.
        folder_data = {"folder": folder, "attributes": {}}
        data = {"request": json.dumps(folder_data)}

        self._api_client._api_request(  # pylint: disable=protected-access
            "webapi.py?action=add_folder", data
        )


class RestApiClient(HttpApiClient):
    """
    A client that uses the modern REST API of checkmk

    The new API client mostly behaves like requests.
    """

    def __init__(self, api_client):
        super().__init__(api_client)
        self._api_supports_tags = self._does_api_support_tags()

    def _does_api_support_tags(self) -> bool:
        """
        Check if the API supports host tags

        This is mostly checking if https://checkmk.com/de/werk/13964 is
        available in the current site.
        """
        try:
            version = self._get_checkmk_version()
        except Exception:  # pylint: disable=broad-except
            # Problem reading the version
            return False

        if version >= (2, 1, 0, 17):
            return True

        # Probably too old
        return False

    def _get_checkmk_version(self) -> tuple:
        """
        Read the checmk version from the API

        Returns a tuple with version information.
        For example `1.2.3p4` will return `(1, 2, 3, 4)`.
        """
        response = self._api_client._session.get("/version")  # pylint: disable=protected-access
        json_response = response.json()

        checkmk_version = json_response["versions"]["checkmk"]

        version, patchrelease = checkmk_version.split("p", 1)
        patchrelease, _ = patchrelease.split(".", 1)  # might trail in .cee
        patchrelease = int(patchrelease)
        major, minor, patch = version.split(".")
        version = (int(major), int(minor), int(patch), int(patchrelease))

        return version

    @property
    def api_supports_tags(self) -> bool:
        return self._api_supports_tags

    def get_host_tags(self) -> List[dict]:
        # Working around limitations of the builtin client to get the
        # required results from the API.

        tag_response = self._api_client._session.get(  # pylint: disable=protected-access
            "/domain-types/host_tag_group/collections/all"
        )
        tag_response_json = tag_response.json()

        all_tags = []
        keys_to_keep = ("id", "title")
        for host_tag_group in tag_response_json["value"]:
            tag = {key: host_tag_group[key] for key in keys_to_keep}
            tag["tags"] = [
                {key: choice[key] for key in keys_to_keep}
                for choice in host_tag_group["extensions"]["tags"]
            ]

            all_tags.append(tag)

        return all_tags

    def get_folders_from_new_hosts(self, hosts: List[dict]) -> Set[str]:
        "Get the folders from the hosts to create."
        return {self.prefix_path(folder_path) for (_, folder_path, _) in hosts}

    @staticmethod
    def prefix_path(path: str) -> str:
        "Making sure that a path is prefixed with path seperator"

        if not path.startswith(PATH_SEPERATOR):
            return f"{PATH_SEPERATOR}{path}"

        return path

    def move_host(self, host: str, path: str) -> tuple:
        """
        Move an existing host to a new path

        The returned tuple contains two elements.
        The first inidicates if the operation was a success.
        The second contains an error message if a problem was encountered.
        If the operation failed the first element is `false` and
        the second value contains the error message.
        """

        def get_host_etag(self, host: str):  # type: ignore[no-untyped-def]
            response = self._api_client._session.get(  # pylint: disable=protected-access
                f"/objects/host_config/{host}",
            )
            return response.headers.get("etag")

        etag = get_host_etag(self, host)

        folder = self.prefix_path(path)

        response = self._api_client._session.post(  # pylint: disable=protected-access
            f"/objects/host_config/{host}/actions/move/invoke",
            headers={"If-Match": f"{etag}", "Content-Type": "application/json"},
            json={"target_folder": folder},
        )

        if response.status_code < 400:
            return (True, None)

        json_response = response.json()
        return (False, json_response)

    def get_folders(self) -> Set[str]:
        root_folder = "/"

        response = self._api_client._session.get(  # pylint: disable=protected-access
            "/domain-types/folder_config/collections/all",
            params={
                "parent": root_folder,
                "recursive": True,
            },
        )
        json_response = response.json()
        all_folders = [value["extensions"]["path"] for value in json_response["value"]]

        return set(all_folders)

    def add_folder(self, folder: str) -> None:
        path, folder_name = folder.rsplit(PATH_SEPERATOR, 1)
        parent_path = self.prefix_path(path)

        folder_data = {
            "name": folder_name,
            "title": folder_name,
            "parent": parent_path,
        }

        response = self._api_client._session.post(  # pylint: disable=protected-access
            "/domain-types/folder_config/collections/all", json=folder_data
        )
        if response.status_code == 400:
            # Usually means that we are missing the parent
            response_json = response.json()
            try:
                problematic_fields = response_json["fields"]
            except KeyError:
                return  # Silently fail

            if "parent" in problematic_fields:
                # We trigger the creation of the parent...
                self.add_folder(parent_path)

                # ...and we re-submit creating the initial folder
                self._api_client._session.post(  # pylint: disable=protected-access
                    "/domain-types/folder_config/collections/all", json=folder_data
                )


class Chunker:
    """
    Split client requests into smaller batch sizes.

    We learned that having a full activation queue might lead to slow
    WATO reaction.
    As a workaround we do not wait until the activation queue is full
    of our requests but submit smaller quantities of changes.

    This class splits the requests into chunks of the desired amount
    and calls the corresponding methods.
    Other methods are not proxied.
    """

    _CHUNKABLE_METHODS = {"delete_hosts"}
    _CHUNKABLE_FUNCTIONS = {"add_hosts", "modify_hosts"}

    def __init__(self, api_client: BaseApiClient, chunk_size: int):
        self._api_client = api_client
        self._chunk_size = chunk_size

        self._chunkable = set.union(self._CHUNKABLE_METHODS, self._CHUNKABLE_FUNCTIONS)

    def __getattr__(self, attr):
        if attr in self._chunkable:
            api_method = getattr(self._api_client, attr)

            if attr in self._CHUNKABLE_METHODS:
                attribute = self._chunk_call(api_method)
            else:
                attribute = self._chunk_returning_call(api_method)
        else:
            attribute = getattr(self._api_client, attr)

        return attribute

    @property
    def requires_activation(self) -> bool:
        "Indicates if an activation needs to be triggered"
        # The wrapped methods activate the changes.
        return False

    @staticmethod
    def chunks(iterable: Iterable, count: int) -> Iterable:
        "Collect data into fixed-length chunks or blocks"
        # chunks('ABCDEFG', 3) --> ABC DEF Gxx"
        args = [iter(iterable)] * count
        return zip_longest(*args)

    def _chunk_returning_call(self, function):
        "Chunk a call that returns values"

        @wraps(function)
        def wrap_function(parameter):
            returned_values: dict = {}
            for chunk in self.chunks(parameter, self._chunk_size):
                single_call_return = function([c for c in chunk if c])

                if single_call_return:
                    for key, value in single_call_return.items():
                        try:
                            returned_values[key].extend(value)
                        except AttributeError:  # possibly a dict
                            returned_values[key].update(value)
                        except KeyError:  # no initial value
                            returned_values[key] = value

                    self._api_client.activate_changes()

            return returned_values

        return wrap_function

    def _chunk_call(self, function):
        "Chunk a call that does not return anything"

        @wraps(function)
        def wrap_function(parameter):
            for chunk in self.chunks(parameter, self._chunk_size):
                function([c for c in chunk if c])
                self._api_client.activate_changes()

        return wrap_function


@connector_registry.register
class FileConnector(Connector):  # pylint: disable=too-few-public-methods
    "The connector that manages the importing"

    @classmethod
    def name(cls) -> str:  # pylint: disable=missing-function-docstring
        return "fileconnector"

    def _execution_interval(self) -> int:
        """Number of seconds to sleep after each phase execution"""
        return self._connection_config.interval

    def _execute_phase1(self) -> Phase1Result:
        """Execute the first synchronization phase"""
        self._logger.info("Execute phase 1")

        importer = self._get_importer()
        importer.import_hosts()
        self._logger.info("Found %i hosts in file", len(importer.hosts))  # type: ignore[arg-type]

        if not importer.fields:
            self._logger.error(
                "Unable to read fields from %r. Is the file empty?",
                self._connection_config.path,
            )
            raise RuntimeError("Unable to detect available fields")

        if not importer.hostname_field:
            self._logger.error(
                "Unable to detect hostname field from %r!",
                self._connection_config.path,
            )
            raise RuntimeError("Unable to detect hostname field")

        assert importer.hosts is not None
        return Phase1Result(
            FileConnectorHosts(importer.hosts, importer.hostname_field, importer.fields),
            self._status,
        )

    def _get_importer(self) -> FileImporter:
        "Get the correct importer based on the current config."
        file_format = self._connection_config.file_format
        if file_format == "csv":
            importer: FileImporter = CSVImporter(
                self._connection_config.path, self._connection_config.csv_delimiter
            )
        elif file_format == "bvq":
            importer = BVQImporter(self._connection_config.path)
        elif file_format == "json":
            importer = JSONImporter(self._connection_config.path)
        else:
            raise RuntimeError(f"Invalid file format {file_format!r}")

        if self._connection_config.lowercase_everything:
            self._logger.info("All imported values will be lowercased")
            importer = LowercaseImporter(importer)  # type: ignore[assignment]

        if self._connection_config.replace_special_chars:
            self._logger.info("All imported values will have their values santized")
            importer = SanitisingImporter(importer)  # type: ignore[assignment]

        return importer

    def _execute_phase2(self, phase1_result: Phase1Result):  # type: ignore[no-untyped-def]
        """Execute the second synchronization phase

        It is executed based on the information provided by the first phase. This
        phase is intended to talk to the local WATO Web API for updating the
        Check_MK configuration based on the information provided by the connection.
        """
        with self.status.next_step("phase2_extract_result", _("Phase 2.1: Extracting result")):
            if isinstance(phase1_result.connector_object, NullObject):
                raise ValueError("Remote site has not completed phase 1 yet")

            if not isinstance(phase1_result.connector_object, FileConnectorHosts):
                raise ValueError(
                    "Got invalid connector object as phase 1 result: "
                    f"{phase1_result.connector_object!r}"
                )

            cmdb_hosts = phase1_result.connector_object.hosts
            fieldnames = phase1_result.connector_object.fieldnames
            hostname_field = phase1_result.connector_object.hostname_field
            import_contains_ip_addresses = fields_contain_ip_addresses(
                phase1_result.connector_object.fieldnames
            )

        with self.status.next_step("phase2_fetch_hosts", _("Phase 2.2: Fetching existing hosts")):
            self._api_client = self._get_api_client()  # pylint: disable=attribute-defined-outside-init

            cmk_hosts = self._api_client.get_hosts()

            cmk_tags = None
            fields_contain_tags = any(is_tag(name) for name in fieldnames)
            if fields_contain_tags:
                if self._api_client.api_supports_tags:
                    host_tags = self._api_client.get_host_tags()
                    cmk_tags = create_hostlike_tags(host_tags)
                else:
                    self._logger.warning(
                        "The used version of the REST API does not provide the "
                        "required tag access. "
                        "Support for this has been added as of werk 13964. "
                        "Tag sync disabled."
                    )

        with self.status.next_step("phase2_update", _("Phase 2.3: Updating config")) as step:
            hosts_changed, change_message = self._update_config(
                cmdb_hosts,
                cmk_hosts,
                hostname_field,
                cmk_tags,
                import_contains_ip_addresses,
            )
            self._logger.info(change_message)
            step.finish(change_message)

        with self.status.next_step("phase2_activate", _("Phase 2.4: Activating changes")) as step:
            if hosts_changed and self._api_client.requires_activation:
                if self._activate_changes():
                    step.finish(_("Activated the changes"))
                else:
                    step.finish(_("Not activated"))
            else:
                step.finish(_("No activation needed"))

    def load_config(self, config: ConnectorConfigModel) -> None:
        """Load the configuration for this connection"""
        self._connection_config = FileConnectorConfig.load(config)

    def _get_api_client(self):
        "Get a preconfigured API client"

        # The following lines can be used to debug _api_client:
        # self._logger.info("Dir: {}".format(dir(self._web_api)))
        # # Check method signature:
        # import inspect
        # self._logger.info("Sig: {}".format(inspect.getargspec(self._web_api._api_request)))

        def is_http_client(dcd_client) -> bool:  # type: ignore[no-untyped-def]
            "Checking if the client is for the HTTP API implementation"

            # Attributes only present at old client:
            # {'_http_post', '_api_request', '_parse_api_response',
            #  'execute_remote_automation', 'edit_host'}
            # We only check for the one used for direct API access:
            if hasattr(dcd_client, "_api_request"):
                return True

            return False

        if is_http_client(self._client):
            self._logger.debug("Creating a HttpApiClient")
            api_client = HttpApiClient(self._client)
        else:
            self._logger.debug("Creating a RestApiClient")
            api_client = RestApiClient(self._client)

        chunk_size = self._connection_config.chunk_size
        if chunk_size:
            self._logger.info("Processing in chunks of %i", chunk_size)
            api_client = Chunker(api_client, chunk_size)  # type: ignore[assignment]

        return api_client

    def _update_config(  # type: ignore[no-untyped-def]
        self, cmdb_hosts, cmk_hosts, hostname_field, cmk_tags, update_ips: bool = False
    ):
        hosts_to_create, hosts_to_modify, hosts_to_delete, hosts_to_move = self._partition_hosts(
            cmdb_hosts, cmk_hosts, hostname_field, cmk_tags, update_ips
        )  # type: ignore[misc]

        if self._connection_config.label_path_template:
            # Creating possibly missing folders if we rely on
            # labels for the path creation.
            self._process_folders(hosts_to_create)
            self._process_folders(hosts_to_move)

        created_host_names = self._create_new_hosts(hosts_to_create)
        modified_host_names = self._modify_existing_hosts(hosts_to_modify)
        deleted_host_names = self._delete_hosts(hosts_to_delete)
        moved_host_names = self._move_hosts(hosts_to_move)

        changes_to_hosts = bool(
            created_host_names or modified_host_names or deleted_host_names or moved_host_names
        )
        change_message = self._get_change_message(
            created_host_names, modified_host_names, deleted_host_names, moved_host_names
        )

        return changes_to_hosts, change_message

    def _partition_hosts(
        self,
        cmdb_hosts: List[dict],
        cmk_hosts: Dict[str, dict],
        hostname_field: str,
        cmk_tags: Dict[str, List[str]],
        update_ips: bool = False,
    ) -> Tuple[list, list, list]:
        """
        Partition the hosts into three groups:

        1) New hosts which have to be added.
        2) Existing hosts which which have to be modified.
        3) Existing hosts that have been removed from the import.

        Unrelated hosts that are not handled by this connection should never be
        modified. If a host is handled by a connection is determined by the the
        locked attribute. Locked attributes are exclusively set by the connection
        and cannot be modified in the GUI, but other attributes can still be
        modified.
        """
        host_overtake_filters = [
            re.compile(f) for f in self._connection_config.host_overtake_filters
        ]

        def overtake_host(hostname: str) -> bool:
            if not host_overtake_filters:
                return False

            return any(f.match(hostname) for f in host_overtake_filters)

        global_ident = self.global_ident()
        hosts_managed_by_plugin = {}
        hosts_to_overtake = set()
        unrelated_hosts = set()
        for host_name, host in cmk_hosts.items():
            locked_by = host["attributes"].get("locked_by")
            if locked_by == global_ident:
                hosts_managed_by_plugin[host_name] = host
            elif overtake_host(host_name) and not locked_by:
                # A user might want the plugin to overtake already
                # existing hosts. These hosts usually have been added
                # before and their labels shall now be managed by this
                # plugin.
                # To avoid a hostile takeover this only is done for
                # hosts that are not locked by another plugin.
                self._logger.debug("Marking host %r for takeover", host_name)
                hosts_to_overtake.add(host_name)
            else:
                self._logger.debug("Host %r already exists as an unrelated host", host_name)
                unrelated_hosts.add(host_name)

        self._logger.info(
            "Existing hosts: %i managed by this connection, %i unrelated",
            len(hosts_managed_by_plugin),
            len(unrelated_hosts),
        )

        host_filters = [re.compile(f) for f in self._connection_config.host_filters]

        def host_matches_filters(host: str) -> bool:
            if not host_filters:
                return True

            return any(f.match(host) for f in host_filters)

        def add_prefix_to_labels(
            labels: Dict[str, str], prefix: Optional[str] = None
        ) -> Dict[str, str]:
            if not prefix:
                return labels

            return {f"{prefix}{key}": value for key, value in labels.items()}

        def needs_modification(old: dict, new: dict) -> bool:
            for label, value in new.items():
                try:
                    if old[label] != value:
                        self._logger.debug(
                            "Difference detected at %r: %r vs. %r",
                            label,
                            old[label],
                            value,
                        )
                        return True
                except KeyError:
                    self._logger.debug("Missing %s (%r vs. %r)", label, old, new)
                    return True

            return False

        def create_host_tags(host_tags: dict) -> dict:
            tags = {tag_matcher.get_tag(key): value for key, value in host_tags.items()}

            for tag, choice in tags.items():
                try:
                    tag_matcher.is_possible_value(tag, choice, True)
                except ValueError as verr:
                    self._logger.error(verr)

            return tags

        def ip_needs_modification(old_ip: Optional[str], new_ip: Optional[str]) -> bool:
            return old_ip != new_ip

        def clean_cmk_attributes(host: dict) -> dict:
            """
            Creates a cleaned up version of the host attributes dict.

            The aim of this to have a dict comparable with the data
            retrieved from the CMDB import.
            """
            return {
                key: value
                for key, value in host.items()
                if not (key in BUILTIN_ATTRIBUTES or is_tag(key))
            }

        if self._connection_config.label_path_template:
            path_labels = self._connection_config.label_path_template.split(PATH_SEPERATOR)

            def get_dynamic_folder_path(labels: dict, keys: List[str], depth: int) -> str:
                def replace_special_chars(string: str) -> str:
                    return string.replace(" ", "_")

                path = generate_path_from_labels(labels, keys, depth)
                if self._connection_config.folder:
                    # In case the hosts should be added to the main
                    # folder we have '' as value. We do not want to
                    # add it because it disturbs CMKs path processing.
                    path.insert(0, self._connection_config.folder)
                path = (replace_special_chars(p) for p in path)  # type: ignore[assignment]
                return PATH_SEPERATOR.join(path)

            get_folder_path = partial(
                get_dynamic_folder_path, keys=path_labels, depth=len(path_labels)
            )
        else:
            # Keeping the signature of the more complex function
            def get_folder_path(_) -> str:  # type: ignore[no-untyped-def,misc]
                return self._connection_config.folder

        def get_host_creation_tuple(
            host: dict,
            hostname_field: str,
            global_ident: GlobalIdent,
            label_prefix: Optional[str] = None,
        ) -> Tuple[str, str, dict]:
            labels = get_host_label(host, hostname_field)
            folder_path = get_folder_path(labels)
            prefixed_labels = add_prefix_to_labels(labels, label_prefix)

            attributes: dict[str, object] = {
                "labels": prefixed_labels,
                # Lock the host in order to be able to detect hosts
                # that have been created through this plugin.
                "locked_by": global_ident,
            }

            ip_address: str | None = get_ip_address(host)
            if ip_address is not None:
                attributes["ipaddress"] = ip_address

            if tag_matcher is not None:
                tags = create_host_tags(get_host_tags(host))
                attributes.update(tags)

            attributes_from_cmdb = get_host_attributes(host)
            attributes.update(attributes_from_cmdb)

            return (hostname, folder_path, attributes)

        def get_host_move_tuple(
            existing_host: dict,
            cmdb_host: dict,
            hostname_field: str,
        ) -> Tuple[str, str]:
            hostname = normalize_hostname(cmdb_host[hostname_field])

            future_label = get_host_label(cmdb_host, hostname_field)
            future_folder_path = get_folder_path(future_label)

            folder_path = existing_host["folder"]
            absolute_future_folder_path = f"/{future_folder_path}"

            self._logger.debug(f"Old Path: {folder_path}; New Path: {absolute_future_folder_path}")

            if folder_path != absolute_future_folder_path:
                self._logger.debug("Folder paths require update")
                return (hostname, future_folder_path)
            return tuple()  # type: ignore[return-value]

        def get_host_modification_tuple(
            existing_host: dict,
            cmdb_host: dict,
            hostname_field: str,
            overtake_host: bool,
            label_prefix: Optional[str] = None,
        ) -> Tuple[str, dict, list]:
            hostname = normalize_hostname(cmdb_host[hostname_field])
            attributes = existing_host["attributes"]

            future_attributes = get_host_attributes(cmdb_host)
            comparable_attributes = clean_cmk_attributes(attributes)

            api_label = attributes.get("labels", {})

            future_label = get_host_label(cmdb_host, hostname_field)
            future_label = add_prefix_to_labels(future_label, label_prefix)

            if label_prefix:
                # We only manage labels that match our prefix
                unmodified_api_label = api_label.copy()
                api_label = {
                    key: value for key, value in api_label.items() if key.startswith(label_prefix)
                }

            if tag_matcher is not None:
                api_tags = get_host_tags(attributes)
                host_tags = get_host_tags(cmdb_host)
                future_tags = create_host_tags(host_tags)

            existing_ip = attributes.get("ipaddress")
            future_ip = get_ip_address(cmdb_host)

            overtake_host = hostname in hosts_to_overtake

            def update_needed() -> bool:
                if overtake_host:
                    self._logger.debug("Host marked for overtake")
                    return True

                if needs_modification(comparable_attributes, future_attributes):
                    self._logger.debug("Attributes require update")
                    return True

                if needs_modification(api_label, future_label):
                    self._logger.debug("Labels require update")
                    return True

                if tag_matcher is not None and needs_modification(api_tags, future_tags):
                    self._logger.debug("Tags require update")
                    return True

                if update_ips and ip_needs_modification(existing_ip, future_ip):
                    self._logger.debug("IP requires update")
                    return True

                return False  # Nothing changed

            if update_needed():
                if label_prefix:
                    unmodified_api_label.update(api_label)
                    api_label = unmodified_api_label

                api_label.update(future_label)
                attributes["labels"] = api_label

                attributes_to_unset = []
                if update_ips:
                    if future_ip is None:
                        if existing_ip is not None:
                            attributes_to_unset.append("ipaddress")
                    else:
                        attributes["ipaddress"] = future_ip

                if tag_matcher is not None:
                    attributes.update(future_tags)

                attributes.update(future_attributes)

                if overtake_host:
                    self._logger.info("Overtaking host %r", hostname)
                    attributes["locked_by"] = global_ident

                try:
                    del attributes["hostname"]
                    self._logger.debug(
                        "Host %r contained attribute 'hostname'. Original data: %r",
                        hostname,
                        cmdb_host,
                    )
                except KeyError:
                    pass  # Nothing to do

                return (hostname, attributes, attributes_to_unset)

            return tuple()  # type: ignore[return-value]

        tag_matcher = TagMatcher(cmk_tags) if cmk_tags is not None else None
        hosts_to_create = []
        hosts_to_modify = []
        hosts_to_move = []
        for host in cmdb_hosts:
            hostname = normalize_hostname(host[hostname_field])
            if not host_matches_filters(hostname):
                continue

            try:
                existing_host = cmk_hosts[hostname]
                if hostname in unrelated_hosts:
                    continue  # not managed by this plugin
            except KeyError:  # Host is missing and has to be created
                self._logger.debug("Creating new host %s", hostname)
                creation_tuple = get_host_creation_tuple(
                    host,
                    hostname_field,
                    global_ident,
                    label_prefix=self._connection_config.label_prefix,
                )
                hosts_to_create.append(creation_tuple)
                continue

            self._logger.debug("Checking managed host %s", hostname)
            host_modifications = get_host_modification_tuple(
                existing_host,
                host,
                hostname_field,
                overtake_host=bool(hostname in hosts_to_overtake),
                label_prefix=self._connection_config.label_prefix,
            )
            if host_modifications:  # Else no changes
                hosts_to_modify.append(host_modifications)

            self._logger.debug("Checking folder of managed host %s", hostname)
            host_move = get_host_move_tuple(existing_host, host, hostname_field)
            if host_move:  # Else no changes
                hosts_to_move.append(host_move)

        cmdb_hostnames = set(normalize_hostname(host[hostname_field]) for host in cmdb_hosts)
        # API requires this to be a list
        hosts_to_delete = list(set(hosts_managed_by_plugin) - cmdb_hostnames)

        self._logger.info(
            "Planned host actions: %i to create, %i to modify, %i to move, %i to delete",
            len(hosts_to_create),
            len(hosts_to_modify),
            len(hosts_to_move),
            len(hosts_to_delete),
        )

        return hosts_to_create, hosts_to_modify, hosts_to_delete, hosts_to_move  # type: ignore[return-value]

    def _process_folders(self, hosts: List[dict]):  # type: ignore[no-untyped-def]
        # Folders are represented as a string.
        # Paths for the HTTP API are written Unix style without prefixed slash: 'folder/subfolder'
        # When using the REST API they have to be '/folder/subfolder'
        # We let the API client decide how the folders should be formatted.
        host_folders = self._api_client.get_folders_from_new_hosts(hosts)
        self._logger.debug(
            "Found the following folders from missing or to be moved hosts: %s", host_folders
        )
        existing_folders = self._api_client.get_folders()
        self._logger.debug("Existing folders: %s", existing_folders)

        folders_to_create = host_folders - existing_folders
        self._logger.debug("Creating the following folders: %s", folders_to_create)
        self._create_folders(sorted(folders_to_create))

    def _create_folders(self, folders: List[str]) -> List[str]:
        if not folders:
            self._logger.debug("No folders to create.")
            return []

        self._logger.debug("Creating the following folders: %s", folders)

        created_folders = []
        for folder in folders:
            self._logger.info("Creating folder: %s", folder)
            self._api_client.add_folder(folder)
            created_folders.append(folder)

        # We want our folders to exist before processing the hosts
        self._activate_changes()
        self._wait_for_folders(folders)

        return created_folders

    def _wait_for_folders(self, folders: List[str]):  # type: ignore[no-untyped-def]
        self._logger.debug("Waiting for folders to be created")
        timeout = 60  # seconds
        interval = 2  # seconds
        start = time.time()

        def are_folders_missing() -> bool:
            existing_folders = self._api_client.get_folders()
            missing_folders = set(folders) - existing_folders
            self._logger.debug("Missing the following folders: %s", ", ".join(missing_folders))
            return bool(missing_folders)

        def get_duration() -> int:
            return int(time.time() - start)

        while are_folders_missing() and get_duration() < timeout:
            time.sleep(interval)

        if get_duration() > timeout:
            self._logger.debug("Timed out after waiting %is for folders to be created.", timeout)

    def _create_new_hosts(self, hosts_to_create: List[tuple]) -> List[str]:
        if not hosts_to_create:
            self._logger.debug("Nothing to create")
            return []

        created_host_names = self._create_hosts(hosts_to_create)

        self._logger.debug("Created %i hosts", len(created_host_names))
        if not created_host_names:
            return []

        if self._connection_config.use_service_discovery:
            self._discover_hosts(created_host_names)

        return created_host_names

    def _create_hosts(self, hosts_to_create: List[tuple]) -> List[str]:
        self._logger.debug(
            "Creating %i hosts (%s)",
            len(hosts_to_create),
            ", ".join(h[0] for h in hosts_to_create),
        )
        result = self._api_client.add_hosts(hosts_to_create)

        for hostname, message in sorted(result["failed_hosts"].items()):
            self._logger.error('Creation of "%s" failed: %s', hostname, message)

        return result["succeeded_hosts"]

    def _discover_hosts(self, host_names_to_discover: List[str]):  # type: ignore[no-untyped-def]
        self._logger.debug(
            "Discovering services on %i hosts (%s)",
            len(host_names_to_discover),
            host_names_to_discover,
        )
        self._api_client.discover_services(host_names_to_discover)
        self._wait_for_bulk_discovery()

    def _wait_for_bulk_discovery(self):
        self._logger.debug("Waiting for bulk discovery to complete")
        timeout = 60  # seconds
        interval = 0.5  # seconds
        start = time.time()

        def discovery_stopped() -> bool:
            return self._api_client.is_discovery_running() is False

        def get_duration() -> int:
            return int(time.time() - start)

        while not discovery_stopped() and get_duration() < timeout:
            time.sleep(interval)

        if not discovery_stopped():
            self._logger.error(
                "Timeout out waiting for the bulk discovery to finish (Timeout: %d sec)",
                timeout,
            )
        else:
            self._logger.debug("Bulk discovery finished after %0.2f seconds", get_duration())

    def _modify_existing_hosts(self, hosts_to_modify: List[tuple]) -> List[str]:
        """
        Modify the given hosts. Returns the IDs of modified hosts.

        Will chunk the given hosts if necessary.
        """
        if not hosts_to_modify:
            self._logger.debug("Nothing to modify")
            return []

        modified_host_names = self._modify_hosts(hosts_to_modify)

        self._logger.debug("Modified %i hosts", len(modified_host_names))
        return modified_host_names

    def _move_hosts(self, hosts_to_move: List[tuple]) -> List[str]:
        "Moves the given hosts. Returns the IDs of moved hosts."
        self._logger.debug(
            "Moving %i hosts (%s)",
            len(hosts_to_move),
            ", ".join(h[0] for h in hosts_to_move),
        )
        succeeded = []
        for host, path in hosts_to_move:
            result, error = self._api_client.move_host(host, path)

            if result:
                succeeded.append(host)
                self._logger.info('Moved "%s" to %s', host, path)
            else:
                self._logger.error('Moving of "%s" failed: %s', host, error)

        return succeeded

    def _modify_hosts(self, hosts_to_modify: List[tuple]) -> List[str]:
        "Modify the given hosts. Returns the IDs of modified hosts."
        self._logger.debug(
            "Modifying %i hosts (%s)",
            len(hosts_to_modify),
            ", ".join(h[0] for h in hosts_to_modify),
        )
        result = self._api_client.modify_hosts(hosts_to_modify)

        for hostname, message in sorted(result["failed_hosts"].items()):
            self._logger.error('Modification of "%s" failed: %s', hostname, message)

        return result["succeeded_hosts"]

    def _delete_hosts(self, hosts_to_delete: List[str]) -> List[str]:
        """Delete hosts that have been created by this connection and are not existing anymore"""
        if not hosts_to_delete:
            self._logger.debug("Nothing to delete")
            return []

        self._api_client.delete_hosts(hosts_to_delete)

        self._logger.debug(
            "Deleted %i hosts (%s)", len(hosts_to_delete), ", ".join(hosts_to_delete)
        )

        return hosts_to_delete

    @staticmethod
    def _get_change_message(
        created_host_names: list,
        modified_host_names: list,
        deleted_host_names: list,
        moved_host_names: list,
    ) -> str:
        "Get a message describing the changes that have been performed"
        changes_to_hosts = bool(
            created_host_names or modified_host_names or moved_host_names or deleted_host_names
        )
        if changes_to_hosts:
            messages = []
            if created_host_names:
                messages.append(_("%i created") % len(created_host_names))
            if modified_host_names:
                messages.append(_("%i modified") % len(modified_host_names))
            if moved_host_names:
                messages.append(_("%i moved") % len(moved_host_names))
            if deleted_host_names:
                messages.append(_("%i deleted") % len(deleted_host_names))

            change_message = _("Hosts: %s") % ", ".join(messages)
        else:
            change_message = _("Nothing changed")

        return change_message

    def _activate_changes(self) -> bool:
        "Activate changes. Returns a boolean representation of the success."
        self._logger.debug("Activating changes")
        changes_activated = self._api_client.activate_changes()
        if not changes_activated:
            self._logger.info(_("There was no change to activate"))

        return changes_activated

    def _get_site_changes(self, phase1_result: Phase1Result) -> None:
        """Intentionally raise exception"""
        raise NotImplementedError()


class TagMatcher:
    """
    Tag matching with some additonal logic.

    It is unclear if the casing of the received data will match the
    casing in CMK. Therefore we can search for matching tags in a
    case-insensitive way.

    Looking for a matching tag is always done as following:
    * If there is a tag matching our casing we use this.
    * If there is a tag with a different casing we use this.
    * If no matching tag is found throw an error.
    """

    def __init__(self, tags: dict):
        self._original = tags
        self._normalized_names = {key.lower(): key for key in tags}

    def get_tag(self, name: str) -> str:
        """
        Get the matching tag independent of used casing.

        Throw a `ValueError` if no tag matches.
        """
        if name in self._original:
            return name

        try:
            return self._normalized_names[name.lower()]
        except KeyError as kerr:
            raise ValueError(f"No matching tag for {name!r} found!") from kerr

    def is_possible_value(self, tag: str, value: str, raise_error: bool = False) -> bool:
        "Check if the value is possible for the given tag"

        tag = self.get_tag(tag)
        values = self._original[tag]
        match_found = value in values

        if raise_error and not match_found:
            raise ValueError(
                f"{value!r} is no possible choice for tag {tag}. " "Valid tags are: {}".format(
                    ", ".join(values)
                )
            )

        return match_found


def generate_path_from_labels(labels: dict, keys: List[str], depth: int = 0) -> List[str]:
    "Generate a path from the given labels"
    if not labels:
        if not depth:
            depth = 0

        return [FOLDER_PLACEHOLDER] * depth

    # A host might have the label set without a value.
    # In this case we want to use the placeholder.
    path = [labels.get(key) or FOLDER_PLACEHOLDER for key in keys]

    return path


class FileConnectorHosts(ConnectorObject):
    "Class used for exchanging data between different stages"

    def __init__(self, hosts: List[dict], hostname_field: str, fieldnames: List[str]):
        self.hosts = hosts
        self.hostname_field = hostname_field
        self.fieldnames = fieldnames

    @classmethod
    def from_serialized_attributes(cls, serialized: dict):  # type: ignore[no-untyped-def]
        "Generate an instance from serialized attributes"
        return cls(serialized["hosts"], serialized["hostname_field"], serialized["fieldnames"])

    def _serialize_attributes(self) -> dict:
        "Serialize class attributes"
        return {
            "hosts": self.hosts,
            "hostname_field": self.hostname_field,
            "fieldnames": self.fieldnames,
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self.hosts!r}, "
            f"{self.hostname_field!r}, {self.fieldnames!r})"
        )
