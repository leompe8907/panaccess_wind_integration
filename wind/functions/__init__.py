"""
Módulo de funciones para la aplicación wind.

Este módulo exporta todas las funciones organizadas por archivo.
"""
from wind.functions.login import panaccess_session_status_view
from wind.functions.singleton import singleton
from wind.functions.logged_in import logged_in_view
from wind.functions.sync_subscribers import (
    sync_subscribers_view,
    compare_and_update_subscribers_view
)
from wind.functions.sync_products import (
    sync_products_view,
    test_call_list_products,
    products_stats_view,
)
from wind.functions.sync_smartcards import (
    sync_smartcards_view,
    test_call_list_smartcards,
    smartcards_stats_view,
)
from wind.functions.create_subscriber import create_subscriber_view
from wind.functions.validate_subscriber_email import validate_subscriber_email_view
from wind.functions.change_password import change_password_view
from wind.functions.full_sync import full_sync_view

# Importar funciones de smartcards para facilitar el acceso
from wind.functions.getSmartcard import (
    sync_smartcards,
    fetch_all_smartcards,
    download_smartcards_since_last,
    compare_and_update_all_smartcards,
    CallListSmartcards,
    DataBaseEmpty as SmartcardDataBaseEmpty,
    LastSmartcard
)

# Importar funciones de login info
from wind.functions.getSubscriberLoginInfo import (
    sync_subscribers_login_info,
    fetch_all_subscribers_login_info,
    fetch_login_info_for_subscriber,
    CallGetSubscriberLoginInfo,
    DataBaseEmpty as LoginInfoDataBaseEmpty,
)

# Importar funciones de productos
from wind.functions.getProducts import (
    sync_products,
    fetch_all_products,
    download_products_since_last,
    compare_and_update_all_products,
    CallListOfProducts,
    DataBaseEmpty as ProductsDataBaseEmpty,
    LastProduct,
)

__all__ = [
    'panaccess_session_status_view',
    'singleton',
    'logged_in_view',
    'sync_subscribers_view',
    'compare_and_update_subscribers_view',
    'sync_smartcards',
    'fetch_all_smartcards',
    'download_smartcards_since_last',
    'compare_and_update_all_smartcards',
    'CallListSmartcards',
    'SmartcardDataBaseEmpty',
    'LastSmartcard',
    'sync_subscribers_login_info',
    'fetch_all_subscribers_login_info',
    'fetch_login_info_for_subscriber',
    'CallGetSubscriberLoginInfo',
    'LoginInfoDataBaseEmpty',
    'sync_products',
    'fetch_all_products',
    'download_products_since_last',
    'compare_and_update_all_products',
    'CallListOfProducts',
    'ProductsDataBaseEmpty',
    'LastProduct',
    'sync_products_view',
    'test_call_list_products',
    'products_stats_view',
    'sync_smartcards_view',
    'test_call_list_smartcards',
    'smartcards_stats_view',
    'create_subscriber_view',
    'validate_subscriber_email_view',
    'change_password_view',
    'full_sync_view',
]
