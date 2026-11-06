from django.db import migrations, models


def convert_legacy_roles(apps, schema_editor):
    MemberProfile = apps.get_model("fund", "MemberProfile")
    MemberProfile.objects.filter(role__in=("treasurer", "bookkeeper")).update(
        role="member"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("fund", "0006_contributionpenalty_paid_at_and_more"),
    ]

    operations = [
        migrations.RunPython(convert_legacy_roles, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="memberprofile",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("member", "Member"), ("client", "Client")],
                default="member",
                max_length=16,
            ),
        ),
    ]
