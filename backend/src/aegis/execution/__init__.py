"""Safe execution of remediation actions.

The only way from a model's suggestion to a change in the environment runs
through this package, and it runs in one direction:

    ActionProposal -> ActionGate.validate -> ValidatedAction -> ExecutionService

``ValidatedAction`` cannot be constructed outside ``ActionGate``, so there is no
second route. Import anything you like from here; none of it lets an agent skip
a gate.
"""

from aegis.execution.approvals import ApprovalRequest, ApprovalStore, Decision
from aegis.execution.executors import ExecutionOutcome, ExecutionPorts, Executor
from aegis.execution.leases import Lease, LeaseManager
from aegis.execution.ports import (
    CachePort,
    DeploymentInfo,
    InstanceInfo,
    OperationResult,
    RuntimeReadPort,
    RuntimeWritePort,
)
from aegis.execution.registry import executable_action_types, executor_for, has_executor
from aegis.execution.sandbox import SandboxResult, SandboxRunner, SandboxSpec
from aegis.execution.service import ExecutionReport, ExecutionService
from aegis.execution.validated import ActionGate, GateRejection, ValidatedAction

__all__ = [
    "ActionGate",
    "ApprovalRequest",
    "ApprovalStore",
    "CachePort",
    "Decision",
    "DeploymentInfo",
    "ExecutionOutcome",
    "ExecutionPorts",
    "ExecutionReport",
    "ExecutionService",
    "Executor",
    "GateRejection",
    "InstanceInfo",
    "Lease",
    "LeaseManager",
    "OperationResult",
    "RuntimeReadPort",
    "RuntimeWritePort",
    "SandboxResult",
    "SandboxRunner",
    "SandboxSpec",
    "ValidatedAction",
    "executable_action_types",
    "executor_for",
    "has_executor",
]
