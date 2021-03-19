# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""Helper methods and structures for cleanly interacting with the K8s
CustomResource APIs.
"""

import logging
import base64
from time import sleep
from typing import Dict, Optional, Union
from dataclasses import dataclass
from kubernetes import config, client
from kubernetes.client.api_client import ApiClient
from kubernetes.client.rest import ApiException

_k8s_api_client = None


@dataclass
class CustomResourceReference:
    """Stores a reference to a CustomResource within the cluster.

    Rather than repeating each field of the custom resource reference each time,
    we can pass around a single class with all the fields specified.
    """

    group: str
    version: str
    plural: str
    name: str
    namespace: Optional[str] = None

    def __str__(self):
        return self.to_short_resource_string()

    @property
    def _printable_namespace(self):
        return self.namespace or "cluster"

    def to_short_resource_string(self):
        return f"{self._printable_namespace}:{self.name}"

    def to_long_resource_string(self):
        return f"{self.plural}.{self.version}.{self.group}/{self._printable_namespace}:{self.name}"


def _get_k8s_api_client() -> ApiClient:
    global _k8s_api_client
    if _k8s_api_client is None:
        _k8s_api_client = config.new_client_from_config()
    return _k8s_api_client


def create_k8s_namespace(namespace_name: str):
    _api_client = _get_k8s_api_client()
    return client.CoreV1Api(_api_client).create_namespace(
        client.V1Namespace(name=namespace_name))


def delete_k8s_namespace(namespace_name: str):
    _api_client = _get_k8s_api_client()
    return client.CoreV1Api(_api_client).delete_namespace(namespace_name)


def create_custom_resource(
        reference: CustomResourceReference, custom_resource: dict):
    _api_client = _get_k8s_api_client()
    _api = client.CustomObjectsApi(_api_client)

    if reference.namespace is None:
        return _api.create_cluster_custom_object(
            reference.group, reference.version, reference.plural, custom_resource)
    return _api.create_namespaced_custom_object(
        reference.group, reference.version, reference.namespace, reference.plural, custom_resource)

def patch_custom_resource(
    reference: CustomResourceReference, custom_resource: dict):
    _api_client = _get_k8s_api_client()
    _api = client.CustomObjectsApi(_api_client)

    if reference.namespace is None:
        return _api.patch_cluster_custom_object(
            reference.group, reference.version, reference.plural, reference.name, custom_resource)
    return _api.patch_namespaced_custom_object(
        reference.group, reference.version, reference.namespace, reference.plural, reference.name, custom_resource)

def delete_custom_resource(
    reference: CustomResourceReference, wait_periods: int = 1, period_length: int = 5):
    """Delete custom resource from cluster and wait for it to be removed by the server
    for wait_periods * period_length seconds.

    Returns:
        response, bool:
        response is APIserver response for the operation.
        bool is true if resource was removed from the server and false otherwise
    """
    _api_client = _get_k8s_api_client()
    _api = client.CustomObjectsApi(_api_client)

    _response = None
    if reference.namespace is None:
        _response = _api.delete_cluster_custom_object(
            reference.group, reference.version, reference.plural, reference.name)
    _response = _api.delete_namespaced_custom_object(
        reference.group, reference.version, reference.namespace, reference.plural, reference.name)

    for _ in range(wait_periods):
        sleep(period_length)
        if not get_resource_exists(reference):
            return _response, True

    logging.error(
        f"Wait for resource {reference} to be removed by server timed out")
    return _response, False


def get_resource(reference: CustomResourceReference):
    """Get the resource from a given reference.

    Returns:
        None or object: None if the resource doesnt exist in server, otherwise the
            custom object.
    """
    _api_client = _get_k8s_api_client()
    _api = client.CustomObjectsApi(_api_client)

    if reference.namespace is None:
        return _api.get_cluster_custom_object(
            reference.group, reference.version, reference.plural, reference.name)

    return _api.get_namespaced_custom_object(
        reference.group, reference.version, reference.namespace, reference.plural, reference.name)


def get_resource_exists(reference: CustomResourceReference) -> bool:
    try:
        return get_resource(reference) is not None
    except ApiException:
        return False


def wait_resource_consumed_by_controller(
        reference: CustomResourceReference, wait_periods: int = 3, period_length: int = 10):
    if not get_resource_exists(reference):
        logging.error(f"Resource {reference} does not exist")
        return None

    for _ in range(wait_periods):
        resource = get_resource(reference)

        if 'status' in resource:
            return resource

        sleep(period_length)

    logging.error(
        f"Wait for resource {reference} to be consumed by controller timed out")
    return None

def _get_terminal_condition(resource: object) -> Union[None, bool]:
    """Get the .status.ACK.Terminal boolean from a given resource.

    Returns:
        None or bool: None if the status field doesn't exist, otherwise the
            field value cast to a boolean (default False).
    """
    if 'status' not in resource or 'conditions' not in resource['status']:
        return None

    conditions: Dict = resource['status']['conditions']
    if 'ACK' not in conditions or 'Terminal' not in conditions['ACK']:
        return None

    terminal: Dict = conditions['ACK']['Terminal']
    return bool(terminal.get('status', False))

def get_resource_arn(resource: object) -> Union[None, str]:
    """Get the .status.ackResourceMetadata.arn value from a given resource.

    Returns:
        None or string: None if the status field doesn't exist, otherwise the
            field value.
    """
    if 'ackResourceMetadata' in resource['status'] and \
        'arn' in resource['status']['ackResourceMetadata']:
        return resource['status']['ackResourceMetadata']['arn']
    return None


def create_opaque_secret(namespace: str,
                         name: str,
                         key: str,
                         value: str):
    """
    Create new k8 Opaque Secret.

    :param namespace: Namespace of the secret.
    :param name: Name of the secret
    :param key: Key of the secret
    :param value: Value of the secret
    :return: None
    """
    _api_client = _get_k8s_api_client()
    body = client.V1Secret()
    body.api_version = 'v1'
    body.data = {key:base64.b64encode(value.encode('ascii')).decode('utf-8')}
    body.kind = 'Secret'
    body.metadata = {'name': name}
    body.type = 'Opaque'
    body = _api_client.sanitize_for_serialization(body)
    client.CoreV1Api(_api_client).create_namespaced_secret(namespace,body)


def delete_secret(namespace: str,
                  name: str):
    """
    Delete an existing k8 secret.

    :param namespace: Namespace of the secret.
    :param name: Name of the secret
    :return: None
    """
    _api_client = _get_k8s_api_client()
    client.CoreV1Api(_api_client).delete_namespaced_secret(name, namespace)

def wait_on_condition(reference: CustomResourceReference,
                      condition_name: str,
                      desired_condition_status: str,
                      wait_periods: int = 2,
                      period_length: int = 60) -> bool:
    """
    Waits for the specified condition in .status.conditions to reach the desired value.

    Precondition:
        resource must be consumed by the controller (i.e. have a .status field)

    Returns:
        False if the resource doesn't exist, have .status.conditions at all, have the requested
            condition type, or if the wait times out. True otherwise.
    """

    if not get_resource_exists(reference):
        logging.error(f"Resource {reference} does not exist")
        return False

    for i in range(wait_periods):
        logging.debug(f"Waiting on condition {condition_name} to reach {desired_condition_status} for resource {reference} ({i})")
        resource = get_resource(reference)

        if 'conditions' not in resource['status']:
            logging.error(f"Resource {reference} does not have a .status.conditions field.")
            return False

        desired_condition = None
        for condition in resource['status']['conditions']:
            if condition['type'] == condition_name:
                desired_condition = condition

        if not desired_condition:
            logging.error(f"Resource {reference} does not have a condition of type {condition_name}.")
            return False
        else:
            if desired_condition['status'] == desired_condition_status:
                logging.info(f"Condition {condition_name} has status {desired_condition_status}, continuing...")
                return True

        sleep(period_length)

    logging.error(f"Wait for condition {condition_name} to reach status {desired_condition_status} timed out")
    return False

def is_resource_in_terminal_condition(
        reference: CustomResourceReference, expected_substring: str):
    if not get_resource_exists(reference):
        logging.error(f"Resource {reference} does not exist")
        return False

    resource = get_resource(reference)
    terminal_status = _get_terminal_condition(resource)
    # Ensure the status existed
    if terminal_status is None:
        logging.error(f"Expected .ACK.Terminal to exist in {reference}")
        return False

    if not terminal_status:
        logging.error(
            f"Expected terminal condition for resource {reference} to be true")
        return False

    terminal_message = terminal.get('message', None)
    if terminal_message != expected_substring:
        logging.error(f"Resource {reference} has terminal condition set True, but with a different message than expected."
                      f" Expected '{expected_substring}', found '{terminal_message}'")
        return False

    return True
