"""Social-messaging ARN resource ids. No AWS calls."""

from infra.constructs.social_messaging_arns import phone_number_arn, waba_arn

REGION = "us-east-1"
ACCOUNT = "000000000000"


def test_prefixed_context_ids_drop_the_api_prefix() -> None:
    assert waba_arn(REGION, ACCOUNT, "waba-0000000000000000") == (
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:waba/0000000000000000"
    )
    assert phone_number_arn(REGION, ACCOUNT, "phone-number-id-1111111111111111") == (
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:phone-number-id/1111111111111111"
    )


def test_ids_without_a_prefix_stay_bare() -> None:
    waba_id = "0123456789abcdef0123456789abcdef"
    phone_id = "abcdef0123456789abcdef0123456789"
    assert waba_arn(REGION, ACCOUNT, waba_id) == (
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:waba/{waba_id}"
    )
    assert phone_number_arn(REGION, ACCOUNT, phone_id) == (
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:phone-number-id/{phone_id}"
    )
