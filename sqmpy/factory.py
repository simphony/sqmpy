from flask import Flask
from flask.ext.wtf.csrf import CsrfProtect


def create_app(config_filename=None, **kwargs):
    """
    Application factory
    :param config_filename:
    :return:
    """
    # Initialize flask app
    app = Flask(__name__.split('.')[0], static_url_path='')

    # Import default configs
    app.config.from_object('sqmpy.defaults')

    # Import from environment
    app.config.from_envvar('SQMPY_SETTINGS', silent=True)

    # Load the given config file
    if config_filename:
        app.config.from_pyfile(config_filename, silent=True)

    # Updated with keyword arguments
    app.config.update(kwargs)

    # Register app on db
    from .database import db
    db.init_app(app)

    # Activate CSRF protection
    if app.config.get('CSRF_ENABLED'):
        CsrfProtect(app)

    # Registering blueprints,
    # IMPORTANT: views should be imported before registering blueprints
    import sqmpy.views
    import sqmpy.security.views
    import sqmpy.job.views
    from .security import security_blueprint
    from .job import job_blueprint
    from .views import main_blueprint
    app.register_blueprint(security_blueprint)
    app.register_blueprint(job_blueprint)
    app.register_blueprint(main_blueprint)

    # A global context processor for sub menu items
    @app.context_processor
    def make_navmenu_items():
        if job_blueprint.name in app.blueprints:
            return {'navitems': {job_blueprint.name: job_blueprint.url_prefix}}
        else:
            return {}

    # Create every registered model. BTW `create_all' will check for existence of tables before running CREATE queries.
    if app.debug:
        with app.app_context():
            db.create_all()

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db.session.remove()

    return app