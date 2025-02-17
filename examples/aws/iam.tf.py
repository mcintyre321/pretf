from pretf.api import block, labels


def pretf_blocks(var):

    group = yield block("resource", "aws_iam_group", "pretf", {
        "name": "pretf-aws",
    })

    for name in var.user_names:

        name_label = labels.clean(name)

        user = yield block("resource", "aws_iam_user", name_label, {
            "name": name,
        })

        yield block("resource", "aws_iam_user_group_membership", name_label, {
            "user": user.name,
            "groups": [group.name],
        })

        yield block("output", f"user_{name_label}", {
            "value": user.name,
        })
