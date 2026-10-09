"""Publish a pinned image's host tools through one atomic directory pointer."""
import os
import tempfile
import uuid
from pathlib import Path

from deployment import export_image_tree, image_reference


def validate_tools(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('host tools must be a regular version directory')
    for name in ('upgrade.py', 'release-upgrade.py', 'deployment.py', 'publication.py', 'credentials.py', 'automation_tools.py', 'maintain.py'):
        if not (directory / name).is_file():
            raise ValueError('image is missing the host automation protocol')


def stage_tools(engine, image, link):
    if image_reference(image):
        raise ValueError('host tools require a local immutable image ID')
    link = Path(link)
    link = link.parent.resolve() / link.name
    if os.path.lexists(link) and (not link.is_symlink() or link.resolve().parent != link.parent):
        raise ValueError('host tool pointer must link to a sibling version directory')
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = link.parent / image.removeprefix('sha256:')
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix='.tools-', dir=link.parent) as temporary:
            staged = Path(temporary) / 'deploy'
            export_image_tree(engine, image, '/app/deploy', staged)
            validate_tools(staged)
            for path in staged.rglob('*'):
                if path.is_file():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            os.rename(staged, destination)
    validate_tools(destination)
    return destination


def refresh_tools(engine, image, link):
    link = Path(link)
    link = link.parent.resolve() / link.name
    destination = stage_tools(engine, image, link)
    if link.is_symlink() and link.resolve() == destination:
        return link
    staged_link = link.with_name('.current-' + uuid.uuid4().hex)
    try:
        staged_link.symlink_to(destination.name)
        os.replace(staged_link, link)
        fd = os.open(link.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staged_link.unlink(missing_ok=True)
    return link
