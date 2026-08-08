from flask import Blueprint, request, jsonify
import logging

from ..errors import AppError, InvalidInputError, OperationFailedError
from ..services.nl_service import NLService
from ..utils.auth import require_auth, get_current_user
from ._service_proxy import CurrentAppService

logger = logging.getLogger(__name__)

# Initialize blueprint and services
nl_bp = Blueprint('nl', __name__)


def _build_nl_service(engine, settings):
    return NLService(engine, settings=settings)


nl_service = CurrentAppService("nl", _build_nl_service)

@nl_bp.route('/nl-query', methods=['POST'])
@require_auth
def process_natural_language_query():
    """Process natural language queries and return structured results"""
    try:
        # Get authenticated user
        user = get_current_user()
        
        # Get query from request
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or 'query' not in data:
            raise InvalidInputError("A query is required.")

        query = data['query']
        if not isinstance(query, str) or not query.strip():
            raise InvalidInputError("A non-empty query is required.")
        
        logger.info("NL query from %s (%s): %s", user.get('email'), user.get('uid'), query)
        
        result = nl_service.process_query(query)
        return jsonify(result)
        
    except AppError:
        raise
    except ValueError as error:
        raise InvalidInputError(
            "The natural-language query is invalid.", detail=error
        ) from error
    except RuntimeError as error:
        raise OperationFailedError(
            "The natural-language query service is unavailable.", detail=error
        ) from error
    except Exception as error:
        logger.exception("NL query failed")
        raise OperationFailedError(
            "Failed to process the natural-language query.", detail=error
        ) from error
