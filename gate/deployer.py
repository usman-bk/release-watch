"""Moves traffic by rewriting the load balancer's weighted forward action."""


class AlbWeights:
    def __init__(self, client, listener_arn: str, stable_group_arn: str, canary_group_arn: str):
        self._client = client
        self._listener_arn = listener_arn
        self._stable = stable_group_arn
        self._canary = canary_group_arn

    def set_canary_weight(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"weight must be between 0 and 100, got {percent}")
        self._client.modify_listener(
            ListenerArn=self._listener_arn,
            DefaultActions=[
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": self._stable, "Weight": 100 - percent},
                            {"TargetGroupArn": self._canary, "Weight": percent},
                        ],
                        "TargetGroupStickinessConfig": {"Enabled": False},
                    },
                }
            ],
        )
