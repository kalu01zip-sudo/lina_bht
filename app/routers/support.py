from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.core.supabase_client import supabase

from app.services.email_service import (
    send_support_email
)


router = APIRouter(
    prefix="/support",
    tags=["Profile"]
)


# ======================================
# REQUEST MODEL
# ======================================

class SupportRequest(BaseModel):

    name: str

    email: EmailStr

    subject: str

    about: str


# ======================================
# CREATE SUPPORT TICKET
# ======================================

@router.post("")
async def create_support_ticket(

    data: SupportRequest
):

    try:

        # ==========================
        # SAVE TO SUPABASE
        # ==========================

        response = supabase.table(
            "support_tickets"
        ).insert({

            "name": data.name,

            "email": data.email,

            "subject": data.subject,

            "about": data.about

        }).execute()

        # ==========================
        # SEND EMAIL TO USER
        # ==========================

        send_support_email(

            to_email=data.email,

            subject="Support Request Received",

            body=f"""
Hello {data.name},

We received your support request.

Subject:
{data.subject}

Message:
{data.about}

Our team will contact you soon.

— Lina Support Team
"""
        )

        return {

            "success": True,

            "ticket": response.data[0]
        }

    except Exception as e:

        print("SUPPORT ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to submit support request"
        )