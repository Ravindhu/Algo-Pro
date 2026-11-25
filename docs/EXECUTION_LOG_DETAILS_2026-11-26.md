# Execution log details (owner 2026-11-25/26)

Plain English: every filled order in the dashboard's Execution log now shows what placed it.

Shown per fill: strategy (short label), risk % the order was sized on, and the level of each timeframe it came from:
- MTF, HTF: `agg 1-3` (aggressive slot) or `con 1-2` (conservative slot)
- LTF: `agg` or `con` (the touch)

Short strategy names: engulfing = EG, ocl_keylevel = OCL-OCL, engulfing_fail = EGF,
ocl_sell_fail_top_seq = FOCL-T, ocl_sell_fail_bottom_seq = FOCL-B.

Details text order: Strategy, Risk, MTF, HTF, LTF (each with its risk), Total, POV, Slicer.

How it works:
1. The risk tree numbers each MTF/HTF slot (`risk_tree.Decision.slot`) and passes the chain of levels to each LTF touch (`Decision.context`).
2. When a ladder order is made, its details are stored on the order (`Order.details`, saved with the cycle).
3. When a child fill is booked, the details are saved by broker order id to `data/bot_live/order_details.json`.
4. The dashboard joins tradebook fills to that file by order id. A fill without a record shows a dash (fills from before this change, and any fill the file cannot match).

Limits: the sized risk % is the order's own sizing; the budget slot percentage is not shown. The file keeps the newest 5000 orders.
