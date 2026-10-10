"""Copiers that fax over the network (``copiers.py``) for Providers → the trunk page and the command line."""
from fastapi import APIRouter, Depends

from .access.route_policy import require_permission
from . import copiers


router = APIRouter(prefix='/admin/sip/copiers', tags=['SIP trunk'])


@router.get('', dependencies=[Depends(require_permission('providers:read'))])
def copier_catalog():
    """Which copier makers document SIP fax with T.38, with what to set and where it was read."""
    return {'copiers': copiers.catalog()}
