from app.services.product_scan_history import (
    save_product_scan,
    get_recent_product_scans
)


scan_id = save_product_scan(

    user_id="test_user",

    scan_data={

        "product": {

            "name": "Retinol Serum",

            "brand": "Ordinary"
        },

        "analysis": {

            "overall_score": 71
        }
    }
)

print("SCAN ID:", scan_id)

history = get_recent_product_scans(
    "test_user"
)

print(history)