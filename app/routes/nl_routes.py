from flask import Blueprint, request, jsonify
from sqlalchemy import create_engine
from ..services.nl_service import NLService

# Initialize blueprint and services
nl_bp = Blueprint('nl', __name__)
engine = create_engine('sqlite:///nba_play_types.db')   
nl_service = NLService(engine)

@nl_bp.route('/nl-query', methods=['POST'])
def process_natural_language_query():
    """Process natural language queries and return structured results"""
    try:
        # Get query from request
        data = request.get_json()
        if not data or 'query' not in data:
            return jsonify({'error': 'No query provided'}), 400
        
        query = data['query']
        result = nl_service.process_query(query)
        return jsonify(result)
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        print(f"NL Query Error: {e}")
        return jsonify({'error': f'Failed to process query: {str(e)}'}), 500 