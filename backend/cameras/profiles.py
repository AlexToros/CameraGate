"""Internal source IDs: 1..256 Extra1, 257..512 Main of the same cameras.

Browser ACLs always use physical camera IDs; only authorized fullscreen requests
are mapped into the second range. Both profiles share the native SDK hub/login.
"""
HD_OFFSET=256


def hd_source(camera_id):
    return camera_id+HD_OFFSET


def source_camera(source_id):
    return (source_id-1)%HD_OFFSET+1
