# Generated by Django 2.0.2 on 2018-03-22 10:33

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('pin', '0002_auto_20180302_1417'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='pin',
            name='cumulative_size',
        ),
    ]
