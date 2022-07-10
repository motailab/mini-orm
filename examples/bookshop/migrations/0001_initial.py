"""Migration 0001_initial.

mini_orm made this file with makemigrations. You can change it.
"""

from mini_orm import migrations


class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.CreateTable('authors', [
            migrations.Column('id', 'integer', primary_key=True, null=False),
            migrations.Column('name', 'string', max_length=100, null=False),
        ]),
        migrations.CreateTable('books', [
            migrations.Column('id', 'integer', primary_key=True, null=False),
            migrations.Column('title', 'string', null=False),
            migrations.Column('price', 'float'),
            migrations.Column('author_id', 'integer', references=('authors', 'id'), on_delete='CASCADE'),
        ]),
    ]
