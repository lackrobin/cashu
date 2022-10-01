import base64
import json
import secrets as scrts
import uuid
from typing import List
from loguru import logger

import requests

import cashu.core.b_dhke as b_dhke
from cashu.core.base import (
    BlindedMessage,
    BlindedSignature,
    CheckPayload,
    MeltPayload,
    MintPayloads,
    Proof,
    SplitPayload,
)
from cashu.core.db import Database
from cashu.core.secp import PublicKey
from cashu.core.settings import DEBUG
from cashu.core.split import amount_split
from cashu.wallet.crud import (
    get_proofs,
    invalidate_proof,
    store_proof,
    update_proof_reserved,
    secret_used,
)


class LedgerAPI:
    def __init__(self, url):
        self.url = url

    @staticmethod
    def _get_keys(url):
        resp = requests.get(url + "/keys").json()
        return {
            int(amt): PublicKey(bytes.fromhex(val), raw=True)
            for amt, val in resp.items()
        }

    @staticmethod
    def _get_output_split(amount):
        """Given an amount returns a list of amounts returned e.g. 13 is [1, 4, 8]."""
        bits_amt = bin(amount)[::-1][:-2]
        rv = []
        for (pos, bit) in enumerate(bits_amt):
            if bit == "1":
                rv.append(2**pos)
        return rv

    def _construct_proofs(
        self, promises: List[BlindedSignature], secrets: List[str], rs: List[str]
    ):
        """Returns proofs of promise from promises. Wants secrets and blinding factors rs."""
        proofs = []
        for promise, secret, r in zip(promises, secrets, rs):
            C_ = PublicKey(bytes.fromhex(promise.C_), raw=True)
            C = b_dhke.step3_alice(C_, r, self.keys[promise.amount])
            proof = Proof(amount=promise.amount, C=C.serialize().hex(), secret=secret)
            proofs.append(proof)
        return proofs

    def _generate_secret(self, randombits=128):
        """Returns base64 encoded random string."""
        return scrts.token_urlsafe(randombits // 8)

    def _load_mint(self):
        assert len(
            self.url
        ), "Ledger not initialized correctly: mint URL not specified yet. "
        self.keys = self._get_keys(self.url)
        assert len(self.keys) > 0, "did not receive keys from mint."

    def request_mint(self, amount):
        """Requests a mint from the server and returns Lightning invoice."""
        r = requests.get(self.url + "/mint", params={"amount": amount})
        return r.json()

    @staticmethod
    def _construct_outputs(amounts: List[int], secrets: List[str]):
        """Takes a list of amounts and secrets and returns outputs.
        Outputs are blinded messages `payloads` and blinding factors `rs`"""
        assert len(amounts) == len(
            secrets
        ), f"len(amounts)={len(amounts)} not equal to len(secrets)={len(secrets)}"
        payloads: MintPayloads = MintPayloads()
        rs = []
        for secret, amount in zip(secrets, amounts):
            B_, r = b_dhke.step1_alice(secret)
            rs.append(r)
            payload: BlindedMessage = BlindedMessage(
                amount=amount, B_=B_.serialize().hex()
            )
            payloads.blinded_messages.append(payload)
        return payloads, rs

    async def _check_used_secrets(self, secrets):
        for s in secrets:
            if await secret_used(s, db=self.db):
                raise Exception(f"secret already used: {s}")

    @staticmethod
    def generate_deterministic_secrets(secret, n):
        """`secret` is the base string that will be tweaked n times"""
        return [f"{secret}_{i}" for i in range(n)]

    async def mint(self, amounts, payment_hash=None):
        """Mints new coins and returns a proof of promise."""
        secrets = [self._generate_secret() for s in range(len(amounts))]
        await self._check_used_secrets(secrets)
        payloads, rs = self._construct_outputs(amounts, secrets)

        resp = requests.post(
            self.url + "/mint",
            json=payloads.dict(),
            params={"payment_hash": payment_hash},
        )
        try:
            promises_list = resp.json()
        except:
            if resp.status_code >= 300:
                raise Exception(f"Error: {f'mint returned {resp.status_code}'}")
            else:
                raise Exception("Unkown mint error.")
        if "error" in promises_list:
            raise Exception("Error: {}".format(promises_list["error"]))

        promises = [BlindedSignature.from_dict(p) for p in promises_list]
        return self._construct_proofs(promises, secrets, rs)

    async def split(self, proofs, amount, snd_secret: str = None):
        """Consume proofs and create new promises based on amount split.
        If snd_secret is None, random secrets will be generated for the tokens to keep (fst_outputs)
        and the promises to send (snd_outputs).

        If snd_secret is provided, the wallet will create blinded secrets with those to attach a
        predefined spending condition to the tokens they want to send."""

        total = sum([p["amount"] for p in proofs])
        fst_amt, snd_amt = total - amount, amount
        fst_outputs = amount_split(fst_amt)
        snd_outputs = amount_split(snd_amt)

        amounts = fst_outputs + snd_outputs
        if snd_secret is None:
            logger.debug("Generating random secrets.")
            secrets = [self._generate_secret() for _ in range(len(amounts))]
        else:
            logger.debug(f"Creating proofs with custom secret: {snd_secret}")
            snd_secrets = self.generate_deterministic_secrets(
                snd_secret, len(snd_outputs)
            )
            assert len(snd_secrets) == len(
                snd_outputs
            ), "number of snd_secrets does not match number of ouptus."
            # append predefined secrets (to send) to random secrets (to keep)
            secrets = [
                self._generate_secret() for s in range(len(fst_outputs))
            ] + snd_secrets

        assert len(secrets) == len(
            amounts
        ), "number of secrets does not match number of outputs"
        await self._check_used_secrets(secrets)
        payloads, rs = self._construct_outputs(amounts, secrets)

        split_payload = SplitPayload(proofs=proofs, amount=amount, output_data=payloads)
        resp = requests.post(
            self.url + "/split",
            json=split_payload.dict(),
        )

        try:
            promises_dict = resp.json()
        except:
            if resp.status_code >= 300:
                raise Exception(f"Error: {f'mint returned {resp.status_code}'}")
            else:
                raise Exception("Unkown mint error.")
        if "error" in promises_dict:
            raise Exception("Error: {}".format(promises_dict["error"]))
        promises_fst = [BlindedSignature.from_dict(p) for p in promises_dict["fst"]]
        promises_snd = [BlindedSignature.from_dict(p) for p in promises_dict["snd"]]
        # Construct proofs from promises (i.e., unblind signatures)
        fst_proofs = self._construct_proofs(
            promises_fst, secrets[: len(promises_fst)], rs[: len(promises_fst)]
        )
        snd_proofs = self._construct_proofs(
            promises_snd, secrets[len(promises_fst) :], rs[len(promises_fst) :]
        )

        return fst_proofs, snd_proofs

    async def check_spendable(self, proofs: List[Proof]):
        payload = CheckPayload(proofs=proofs)
        return_dict = requests.post(
            self.url + "/check",
            json=payload.dict(),
        ).json()

        return return_dict

    async def pay_lightning(self, proofs: List[Proof], amount: int, invoice: str):
        payload = MeltPayload(proofs=proofs, amount=amount, invoice=invoice)
        return_dict = requests.post(
            self.url + "/melt",
            json=payload.dict(),
        ).json()
        return return_dict


class Wallet(LedgerAPI):
    """Minimal wallet wrapper."""

    def __init__(self, url: str, db: str, name: str = "no_name"):
        super().__init__(url)
        self.db = Database("wallet", db)
        self.proofs: List[Proof] = []
        self.name = name

    def load_mint(self):
        super()._load_mint()

    async def load_proofs(self):
        self.proofs = await get_proofs(db=self.db)

    async def _store_proofs(self, proofs):
        for proof in proofs:
            await store_proof(proof, db=self.db)

    async def request_mint(self, amount):
        return super().request_mint(amount)

    async def mint(self, amount: int, payment_hash: str = None):
        split = amount_split(amount)
        proofs = await super().mint(split, payment_hash)
        if proofs == []:
            raise Exception("received no proofs.")
        await self._store_proofs(proofs)
        self.proofs += proofs
        return proofs

    async def redeem(self, proofs: List[Proof], snd_secret: str = None):
        if snd_secret:
            logger.debug(f"Redeption secret: {snd_secret}")
            snd_secrets = self.generate_deterministic_secrets(snd_secret, len(proofs))
            assert len(proofs) == len(snd_secrets)
            # overload proofs with custom secrets for redemption
            for p, s in zip(proofs, snd_secrets):
                p.secret = s
        return await self.split(proofs, sum(p["amount"] for p in proofs))

    async def split(self, proofs: List[Proof], amount: int, snd_secret: str = None):
        assert len(proofs) > 0, ValueError("no proofs provided.")
        fst_proofs, snd_proofs = await super().split(proofs, amount, snd_secret)
        if len(fst_proofs) == 0 and len(snd_proofs) == 0:
            raise Exception("received no splits.")
        used_secrets = [p["secret"] for p in proofs]
        self.proofs = list(
            filter(lambda p: p["secret"] not in used_secrets, self.proofs)
        )
        self.proofs += fst_proofs + snd_proofs
        await self._store_proofs(fst_proofs + snd_proofs)
        for proof in proofs:
            await invalidate_proof(proof, db=self.db)
        return fst_proofs, snd_proofs

    async def pay_lightning(self, proofs: List[Proof], amount: int, invoice: str):
        """Pays a lightning invoice"""
        status = await super().pay_lightning(proofs, amount, invoice)
        if status["paid"] == True:
            await self.invalidate(proofs)
        else:
            raise Exception("could not pay invoice.")
        return status["paid"]

    @staticmethod
    async def serialize_proofs(proofs: List[Proof], hide_secrets=False):
        if hide_secrets:
            proofs_serialized = [p.to_dict_no_secret() for p in proofs]
        else:
            proofs_serialized = [p.to_dict() for p in proofs]
        token = base64.urlsafe_b64encode(
            json.dumps(proofs_serialized).encode()
        ).decode()
        return token

    async def split_to_send(self, proofs: List[Proof], amount, snd_secret: str = None):
        """Like self.split but only considers non-reserved tokens."""
        if snd_secret:
            logger.debug(f"Spending conditions: {snd_secret}")
        if len([p for p in proofs if not p.reserved]) <= 0:
            raise Exception("balance too low.")
        return await self.split(
            [p for p in proofs if not p.reserved], amount, snd_secret
        )

    async def set_reserved(self, proofs: List[Proof], reserved: bool):
        """Mark a proof as reserved to avoid reuse or delete marking."""
        uuid_str = str(uuid.uuid1())
        for proof in proofs:
            proof.reserved = True
            await update_proof_reserved(
                proof, reserved=reserved, send_id=uuid_str, db=self.db
            )

    async def check_spendable(self, proofs):
        return await super().check_spendable(proofs)

    async def invalidate(self, proofs):
        """Invalidates all spendable tokens supplied in proofs."""
        spendables = await self.check_spendable(proofs)
        invalidated_proofs = []
        for idx, spendable in spendables.items():
            if not spendable:
                invalidated_proofs.append(proofs[int(idx)])
                await invalidate_proof(proofs[int(idx)], db=self.db)
        invalidate_secrets = [p["secret"] for p in invalidated_proofs]
        self.proofs = list(
            filter(lambda p: p["secret"] not in invalidate_secrets, self.proofs)
        )

    @property
    def balance(self):
        return sum(p["amount"] for p in self.proofs)

    @property
    def available_balance(self):
        return sum(p["amount"] for p in self.proofs if not p.reserved)

    def status(self):
        print(
            f"Balance: {self.balance} sat (available: {self.available_balance} sat in {len([p for p in self.proofs if not p.reserved])} tokens)"
        )

    def proof_amounts(self):
        return [p["amount"] for p in sorted(self.proofs, key=lambda p: p["amount"])]