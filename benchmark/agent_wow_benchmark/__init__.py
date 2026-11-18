"""Harbor EC2 environment with the benchmark worker's first-boot setup."""

from typing import Any

from harbor.environments.ec2 import EC2Environment as HarborEC2Environment


class EC2Environment(HarborEC2Environment):
    def __init__(self, *args: Any, user_data: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._user_data = user_data

    def _run_instances_kwargs(self) -> dict[str, Any]:
        kwargs = super()._run_instances_kwargs()
        # Harbor 0.25.0 does not expose EC2 UserData as a built-in option.
        kwargs["UserData"] = self._user_data
        kwargs["MetadataOptions"] = {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
        }
        for mapping in kwargs.get("BlockDeviceMappings", []):
            mapping["Ebs"]["Encrypted"] = True
        return kwargs
