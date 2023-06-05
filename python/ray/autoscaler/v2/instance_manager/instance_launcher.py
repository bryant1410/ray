import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List

from ray.autoscaler._private.constants import (
    AUTOSCALER_MAX_CONCURRENT_LAUNCHES,
    AUTOSCALER_MAX_LAUNCH_BATCH,
)
from ray.autoscaler.v2.instance_manager.instance_storage import (
    InstanceStorage,
    InstanceUpdatedSuscriber,
    InstanceUpdateEvent,
)
from ray.autoscaler.v2.instance_manager.node_provider import NodeProvider
from ray.core.generated.instance_manager_pb2 import Instance

logger = logging.getLogger(__name__)


class InstanceLauncher(InstanceUpdatedSuscriber):
    """InstanceLauncher is responsible for reconciling the difference between
    node provider and instance storage. It is also responsible for launching new
    nodes and terminating failing nodes.
    """

    def __init__(
        self,
        instance_storage: InstanceStorage,
        node_provider: NodeProvider,
        max_concurrent_requests: int = math.ceil(
            AUTOSCALER_MAX_CONCURRENT_LAUNCHES / float(AUTOSCALER_MAX_LAUNCH_BATCH)
        ),
        max_nodes_per_request: int = AUTOSCALER_MAX_LAUNCH_BATCH,
    ) -> None:
        self._instance_storage = instance_storage
        self._node_provider = node_provider
        self._max_concurrent_requests = max_concurrent_requests
        self._max_nodes_per_request = max_nodes_per_request
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._launch_instance_executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent_requests
        )

    def notify(self, events: List[InstanceUpdateEvent]) -> None:
        # TODO: we should do reconciliation based on events.
        has_new_request = any(
            [event.new_status == Instance.UNKNOWN for event in events]
        )
        if has_new_request:
            self._executor.submit(self._may_launch_new_instances)

    def _may_launch_new_instances(self):
        new_instances, _ = self._instance_storage.get_instances(
            status_filter={Instance.UNKNOWN}
        )

        if not new_instances:
            logger.debug("No instances to launch")
            return

        queued_instances = []
        for instance in new_instances.values():
            instance.status = Instance.QUEUED
            success, version = self._instance_storage.upsert_instance(
                instance, expected_instance_version=instance.version
            )
            if success:
                instance.version = version
                queued_instances.append(instance)
            else:
                logger.error(f"Failed to update {instance} QUEUED")

        instances_by_type = defaultdict(list)
        for instance in queued_instances:
            instances_by_type[instance.instance_type].append(instance)

        for instance_type, instances in instances_by_type.items():
            for i in range(0, len(instances), self._max_nodes_per_request):
                self._launch_instance_executor.submit(
                    self._launch_new_instances_by_type,
                    instance_type,
                    instances[
                        i : min(
                            i + self._max_nodes_per_request,
                            len(instances),
                        )
                    ],
                )

    def _launch_new_instances_by_type(
        self, instance_type: str, instances: List[Instance]
    ) -> int:
        logger.info(f"Launching {len(instances)} instances of type {instance_type}")
        instances_selected = []
        for instance in instances:
            instance.status = Instance.REQUESTED
            result, version = self._instance_storage.upsert_instance(
                instance, expected_instance_version=instance.version
            )
            if not result:
                logger.warn(f"Failed to update instance {instance}")
            instance.version = version
            instances_selected.append(instance)

        if not instances_selected:
            return 0

        created_cloud_instances = self._node_provider.create_nodes(
            instance_type, len(instances_selected)
        )

        assert len(created_cloud_instances) <= len(instances_selected)

        while created_cloud_instances and instances_selected:
            cloud_instance = created_cloud_instances.pop()
            instance = self._instance_storage.pop()
            instance.cloud_instance_id = cloud_instance.cloud_instance_id
            instance.interal_ip = cloud_instance.internal_ip
            instance.external_ip = cloud_instance.external_ip
            instance.status = Instance.ALLOCATED
            instance.ray_status = Instance.RAY_STATUS_UNKOWN

            # update instance status into the storage
            result, _ = self._instance_storage.upsert_instance(
                instance, expected_instance_version=instance.version
            )

            if not result:
                # TODO: this could only happen when the request is canceled.
                logger.warn(f"Failed to update instance {instance}")
                # push the cloud instance back
                created_cloud_instances.append(cloud_instance)

        if created_cloud_instances:
            # instances are leaked, we probably need to terminate them
            self._node_provider.terminate_nodes(
                [instance.cloud_instance_id for instance in created_cloud_instances]
            )

        if instances_selected:
            # instances creation failed, we need to marke them allocation failed.
            for instance in instances_selected:
                instance.status = Instance.ALLOCATION_FAILED
                result, _ = self._instance_storage.upsert_instance(
                    instance, expected_instance_version=instance.version
                )
                # TODO: this could only happen when the request is canceled.
